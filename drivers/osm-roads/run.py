#!/usr/bin/env python3
"""osm-roads driver — the ROAD NETWORK for an AOI, in BOTH tiers of the plan (ADR-0060 L0→L2, Wave-1).

Its ONLY I/O is the kernel broker:
  read   the tenant's space.aoi frame (discovered — never a hardcoded default)
  fetch  the Overpass interpreter (egress-gated to overpass-api.de)
  write  L1 native geometry  geo.osm_feature — one per OSM highway way (LineString), deterministic
         natural-key id (ds:<tenant>:geo/osm_feature/way/<osm_id>) so re-runs upsert
         L2 H3 cells         cell.roads — road centreline length + density per H3 cell (the analytical
         layer City Scan renders); primary road class + segment count per cell.

Road length per cell is accumulated as ways are processed: each segment's length (haversine) is added to
the H3 cell of its midpoint, at the AOI's resolution. density = km of road / km² of cell.

Real OSM only — fails loudly if the AOI is missing or Overpass returns nothing. NO tenant/AOI default: a
driver runs in the context of exactly one tenant (the runtime supplies it); guessing is how one city's
roads land in another's namespace.

Inputs (env): AOI_ID (optional; the pipeline passes it) · TENANT · OVERPASS_URL
"""
import json, math, os, shutil, subprocess, sys, time, urllib.request
sys.path.insert(0, os.path.join(   # components/drivers/osm-roads → up 3 → components → /_common
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_common"))
import ids   # shared id-derivation helper (ADR-0063): id from the natural key + governed identifiers[]

AOI_HINT = os.environ.get("AOI_ID", "")          # the pipeline passes the exact AOI; else we discover the tenant's own
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""   # NO default — a driver never guesses its tenant
AOI_REF = ""                                     # the RESOLVED aoi id (set in main)
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/osm"


def call(path, body):
    """The one and only I/O primitive: POST JSON through the kernel broker."""
    req = urllib.request.Request(
        os.environ["AIRAWAT_API"] + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Run-Token": os.environ["RUN_TOKEN"]},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def unwrap(res):
    """Peel broker envelopes; parse string bodies that are actually JSON."""
    if isinstance(res, dict):
        if "object" in res and isinstance(res["object"], dict):
            return res["object"]
        for k in ("body", "data", "result", "response"):
            if k in res:
                return unwrap(res[k])
        return res
    if isinstance(res, (bytes, str)):
        text = res.decode("utf-8") if isinstance(res, bytes) else res
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text
    return res


def resolve_aoi():
    """The AOI to fill for THIS tenant. An explicit AOI_ID (the pipeline passes it) is honoured ONLY if it
    belongs to the write tenant; otherwise DISCOVER the tenant's own space.aoi. No default AOI, ever."""
    if AOI_HINT:
        ht = AOI_HINT.split(":")[1] if AOI_HINT.count(":") >= 2 else ""
        if ht and ht != TENANT:
            raise SystemExit(f"FATAL: AOI_ID {AOI_HINT} belongs to tenant '{ht}', not write tenant '{TENANT}' — "
                             f"refusing to write one city's roads into another's namespace.")
        o = unwrap(call("/v1/read", {"scope": "space.aoi", "id": AOI_HINT}))
        if not isinstance(o, dict) or len(o.get("bbox") or []) != 4:
            raise SystemExit(f"FATAL: space.aoi {AOI_HINT} missing / no 4-el bbox: {o!r}")
        return o, [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), AOI_HINT
    rows = call("/v1/query", {"scope": "space.aoi"}).get("results") or []
    mine = [(r.get("object") or r) for r in rows
            if str((r.get("object") or r).get("id", "")).startswith(f"ds:{TENANT}:")
            and len((r.get("object") or r).get("bbox") or []) == 4]
    if not mine:
        raise SystemExit(f"FATAL: no space.aoi for tenant '{TENANT}' — run City Setup first. No default AOI.")
    o = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))  # the city, not a sub-AOI
    return o, [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), o["id"]


def fetch_highway_ways(bbox):
    """All `highway` ways inside the bbox, fetched by TILING. A metro-wide Overpass query is rejected (too big)
    and the broker caps a proxied body at 4 MB, so we split the bbox into small tiles, query each, and DEDUP
    ways by osm_id across tile seams. Each tile retries with backoff (Overpass throttles). OSM bbox is
    [west, south, east, north]; Overpass wants (south,west,north,east)."""
    west, south, east, north = bbox
    step = float(os.environ.get("OVERPASS_TILE_DEG", "0.05"))    # ~5.5 km tiles — small enough to stay under 4 MB
    nx = max(1, int(math.ceil((east - west) / step)))
    ny = max(1, int(math.ceil((north - south) / step)))
    print(f"tiling bbox into {nx}x{ny}={nx*ny} Overpass queries (step {step}°)", flush=True)
    seen = {}                                                   # osm_id -> way element (a way on a seam appears in 2 tiles)
    done = failed = 0
    for iy in range(ny):
        for ix in range(nx):
            s = south + iy * step; n = min(north, s + step)
            w = west + ix * step; e = min(east, w + step)
            ql = ("[out:json][timeout:120];"
                  f'way["highway"]({s},{w},{n},{e});'
                  "out tags geom;")
            payload = None
            for attempt in range(4):
                p = unwrap(call("/v1/fetch", {"url": OVERPASS_URL, "method": "POST", "body": ql}))
                if isinstance(p, dict) and isinstance(p.get("elements"), list):
                    payload = p; break
                time.sleep(5 * (attempt + 1))                   # Overpass throttle/timeout → back off and retry
            done += 1
            if payload is None:
                failed += 1
                print(f"  tile {ix},{iy} failed after retries — skipped (partial, no fabrication)", flush=True)
                continue
            for el in payload["elements"]:
                if el.get("type") == "way":
                    seen[el["id"]] = el
        print(f"  …{done}/{nx*ny} tiles · {len(seen)} unique ways so far", flush=True)
    if not seen:
        raise SystemExit("FATAL: Overpass returned no highway ways for any tile")
    if failed:
        print(f"  ⚠ {failed}/{nx*ny} tiles skipped after retries — partial road coverage (no fabrication)", flush=True)
    return list(seen.values())


def ways_from_pbf(pbf_path, bbox):
    """Extract highway ways inside the bbox from a local OSM PBF, using osmium (pyosmium). Returns ways in the
    SAME shape as the Overpass path ({id, tags, geometry:[{lon,lat}]}) so to_feature() is unchanged. This is the
    reliable, Overpass-free path — the tenant uploads the regional PBF once (driver settings) and every run reads
    it locally. Needs pyosmium in the runtime."""
    import osmium
    w, s, e, n = bbox
    class _H(osmium.SimpleHandler):
        def __init__(self):
            super().__init__(); self.ways = []; self._c = 0
        def way(self, way):
            if "highway" not in way.tags:
                return
            coords = []
            try:
                for nd in way.nodes:
                    if nd.location.valid():
                        coords.append({"lon": nd.location.lon, "lat": nd.location.lat})
            except Exception:
                return
            if len(coords) < 2 or not any(w <= c["lon"] <= e and s <= c["lat"] <= n for c in coords):
                return                                            # skip ways with no vertex inside the AOI
            self.ways.append({"type": "way", "id": way.id, "tags": dict(way.tags), "geometry": coords})
            self._c += 1
            if self._c % 100000 == 0:
                print(f"  …scanned {self._c} highway ways so far", flush=True)
    h = _H()
    h.apply_file(pbf_path, locations=True)                        # locations=True → resolve node coords from the PBF
    return h.ways


def _clip_to_bbox(src_path, bbox):
    """If the PBF is large and the `osmium` CLI is present, extract a reference-complete slice for the AOI bbox
    (with a small pad) BEFORE the expensive locations=True pass. A regional/zone PBF otherwise forces pyosmium to
    index every node in the country and blows the 180s host-subprocess cap (HTTP 504). osmium extract uses a
    spatial index and clips in seconds; complete_ways keeps every node of a way crossing the edge so line geometry
    stays intact. No-op (returns src) when the CLI is missing or the file is small. See ADR-0060 / Wave-1."""
    try:
        if not shutil.which("osmium") or os.path.getsize(src_path) < 150 * 1024 * 1024:
            return src_path
    except Exception:
        return src_path
    w, s, e, n = bbox
    pad = 0.03
    dst = src_path + ".clip.osm.pbf"
    box = f"{w-pad},{s-pad},{e+pad},{n+pad}"
    try:
        t0 = time.time()
        subprocess.run(["osmium", "extract", "-b", box, src_path, "-o", dst, "--overwrite", "-s", "complete_ways"],
                       check=True, capture_output=True, timeout=150)
        print(f"  clipped PBF to AOI bbox {box} → {os.path.getsize(dst)//1024//1024} MB in {time.time()-t0:.0f}s (osmium extract)", flush=True)
        return dst
    except Exception as ex:
        print(f"  osmium extract clip skipped ({str(ex)[:80]}) — reading full PBF", flush=True)
        return src_path


def fetch_highway_ways_from_asset(pbf_id, bbox):
    """Pull the uploaded PBF asset from the filestore (via the broker), write it to a temp file, osmium-extract."""
    import base64, tempfile
    res = call("/v1/file/get", {"id": pbf_id})
    raw = base64.b64decode(res.get("content_b64") or "")
    if not raw:
        raise SystemExit(f"FATAL: PBF asset {pbf_id} is empty / not in the filestore")
    tf = tempfile.NamedTemporaryFile(suffix=".osm.pbf", delete=False)
    clipped = None
    try:
        tf.write(raw); tf.close()
        print(f"reading roads from uploaded PBF asset {pbf_id[:20]}… ({len(raw)//1024//1024} MB) via osmium", flush=True)
        path = _clip_to_bbox(tf.name, bbox)
        clipped = path if path != tf.name else None
        return ways_from_pbf(path, bbox)
    finally:
        for p in (tf.name, clipped):
            if p:
                try: os.unlink(p)
                except Exception: pass


def to_feature(el):
    """One OSM way → one geo.osm_feature; None if the way can't become a line."""
    geom = el.get("geometry") or []
    coords = [[float(p["lon"]), float(p["lat"])] for p in geom
              if isinstance(p, dict) and "lon" in p and "lat" in p]
    if len(coords) < 2:
        return None
    tags = el.get("tags") or {}
    osm_id = str(el["id"])
    # The OSM id is the way's NATURAL KEY: it drives the ds: <local> (deterministic → re-runs upsert) AND is
    # recorded in identifiers[] so a road is resolvable by its OSM id (ADR-0063). `entity()` does both.
    return ids.entity(TENANT, "geo/osm_feature/way", "geo.osm_feature",
        natural=[("osm_way", osm_id, "OpenStreetMap")],
        extra={
            "tenant": TENANT,
            "osm_id": osm_id,
            "osm_type": "way",
            "feature": tags.get("highway", "unknown"),   # road class from the highway tag
            "tags": tags,
            "geometry": {"type": "LineString", "coordinates": coords},
            "source": {"aoi": AOI_REF, "endpoint": OVERPASS_URL, "via": "ds:airawat:driver/osm-roads"},
        })


def _haversine_m(a, b):
    """Great-circle length of a segment, [lon,lat] endpoints → metres."""
    (lo1, la1), (lo2, la2) = a, b
    R = 6371000.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dphi, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def main():
    global AOI_REF
    if not TENANT:                                               # no tenant default — a driver never guesses its tenant
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant (the runtime "
                         "supplies it from the confined run token). Refusing to run without one.")
    import h3
    aoi, bbox, res, AOI_REF = resolve_aoi()
    w, s, e, n = bbox
    print(f"tenant={TENANT} · AOI {AOI_REF} · bbox={bbox} · res={res}", flush=True)

    # SOURCE: the uploaded OSM PBF asset (reliable, Overpass-free) if the tenant provided one; else live Overpass.
    pbf_id = os.environ.get("OSM_PBF_FILE_ID", "").strip()
    if pbf_id:
        ways = fetch_highway_ways_from_asset(pbf_id, bbox)
        print(f"osmium extracted {len(ways)} highway ways in the AOI (from the uploaded PBF)", flush=True)
    else:
        ways = fetch_highway_ways(bbox)                          # fallback: live Overpass (rate-limited at metro scale)
        print(f"overpass returned {len(ways)} highway ways", flush=True)

    # WRITE_VECTORS=0 skips the (slow) per-way geo.osm_feature writes when the L1 vectors already exist — the
    # per-cell length is still accumulated in memory, so cell.roads (the L2 analytical layer) is reached in
    # seconds instead of behind a full ~60k-way re-ingest. Use it to (re)compute cell.roads without re-writing L1.
    WRITE_VECTORS = os.environ.get("WRITE_VECTORS", "1") != "0"
    written = skipped = processed = 0
    classes = {}
    cells = {}                                                   # hc -> {"len": m, "seg": n, "classes": {cls: m}}
    # L1 geo.osm_feature is written in BATCHES (/v1/write/batch) — a metro has ~200k ways and one /v1/write per
    # way overran the run-time cap, so a partial (~70%) network was written. Batching writes the FULL set in
    # ~100 calls, well inside the cap. (id is a deterministic way/<osm_id> key, so re-runs upsert cleanly.)
    _batch = []
    def _flush():
        nonlocal written
        if not _batch:
            return
        r = call("/v1/write/batch", {"items": _batch, "asserted_at": NOW}) or {}
        written += int(r.get("written", 0))
        if r.get("errors"):
            print("  batch sample errors:", json.dumps(r["errors"][:2])[:300], flush=True)
        _batch.clear()
    for el in ways:
        feat = to_feature(el)
        if feat is None:
            skipped += 1
            continue
        if WRITE_VECTORS:
            _batch.append({"scope": "geo.osm_feature", "id": feat["id"], "object": feat})   # L1 native line
            if len(_batch) >= 2000:
                _flush()
        processed += 1
        cls = feat["feature"]
        classes[cls] = classes.get(cls, 0) + 1
        coords = feat["geometry"]["coordinates"]
        for i in range(len(coords) - 1):
            a, b = coords[i], coords[i + 1]
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            if not (w <= mx <= e and s <= my <= n):
                continue                                          # segment midpoint outside the AOI → don't attribute it
            hc = h3.latlng_to_cell(my, mx, res)
            L = _haversine_m(a, b)
            d = cells.setdefault(hc, {"len": 0.0, "seg": 0, "classes": {}})
            d["len"] += L; d["seg"] += 1; d["classes"][cls] = d["classes"].get(cls, 0.0) + L

    if WRITE_VECTORS:
        _flush()                                                 # write the final partial batch
    if processed == 0:
        raise SystemExit("FATAL: no usable highway ways found for this AOI")
    print(f"processed {processed} highway ways ({'wrote ' + str(written) + ' geo.osm_feature' if WRITE_VECTORS else 'geo.osm_feature write SKIPPED (WRITE_VECTORS=0)'}); skipped {skipped}", flush=True)
    print("road classes:", json.dumps(dict(sorted(classes.items())), ensure_ascii=False), flush=True)

    # L2: cell.roads — road length + density per H3 cell (the analytical layer City Scan renders).
    wrote_cells = 0
    for hc, d in cells.items():
        la, lo = h3.cell_to_latlng(hc)
        area = h3.cell_area(hc, unit="km^2")
        prim = max(d["classes"], key=d["classes"].get) if d["classes"] else None
        fact = {
            "schema": "cell.roads", "id": f"ds:{TENANT}:cell/roads/{hc}",
            "cell_ref": {"h3_id": hc, "resolution": res, "centroid": [round(lo, 6), round(la, 6)],
                         "boundary": [[round(lo2, 6), round(la2, 6)] for (la2, lo2) in h3.cell_to_boundary(hc)]},
            "road_length_m": round(d["len"], 1),
            "road_density_km_per_km2": round((d["len"] / 1000.0) / area, 3) if area else 0.0,
            "segment_count": d["seg"], "primary_class": prim,
            "method": "osm-overpass", "source_ref": SRC, "layer": "L2", "confidence": 0.9,
            "derived_from": ["osm:overpass:highway"],
            "prov": {"method": "osm-overpass", "confidence": 0.9, "source_ref": SRC,
                     "derived_from": ["osm:overpass:highway"], "observed_at": NOW},
            "observed_at": NOW}
        fact = {k: v for k, v in fact.items() if v is not None}   # omit unset optionals (house rule R4)
        call("/v1/write", {"scope": "cell.roads", "id": fact["id"], "object": fact,
                           "valid_time": {"from": NOW}, "asserted_at": NOW})
        wrote_cells += 1

    print(f"wrote {wrote_cells} cell.roads density cells", flush=True)
    print(f"RESULT: {json.dumps({'source': 'osm-overpass', 'geo_osm_feature': written, 'cell_roads': wrote_cells})}")


if __name__ == "__main__":
    main()
