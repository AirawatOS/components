#!/usr/bin/env python3
"""osm-drainage driver — the DRAINAGE NETWORK for an AOI (T2 flood input, flood-prediction.md).

Sibling of osm-roads: same line extraction, but matches OSM `waterway` ways (drain / ditch / canal / nala /
stream / river) instead of `highway`. Its only I/O is the kernel broker:
  read   the tenant's space.aoi frame (discovered — never a hardcoded default)
  fetch  Overpass (egress-gated) — or read a locally-uploaded OSM PBF if OSM_PBF_FILE_ID is set
  write  geo.hydro_feature — one LineString per waterway way (ds:<tenant>:geo/hydro_feature/way/<osm_id>)
         cell.drainage     — drain length + density per H3 cell (km of drain / km²), the T2 sink-term signal

Real OSM only — fails loudly if the AOI is missing or Overpass returns nothing. NO synthetic fallback, NO
tenant/AOI default (a driver runs in the context of exactly one tenant, supplied by the runtime).

Inputs (env): AOI_ID (optional) · TENANT · OVERPASS_URL · OSM_PBF_FILE_ID (optional)
"""
import json, math, os, shutil, subprocess, sys, time, urllib.request
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_common"))
import ids

AOI_HINT = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
AOI_REF = ""
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/osm"
# OSM waterway values that are DRAINAGE (convey stormwater). Excludes non-drainage waterway tags (dam, weir…).
DRAINAGE = {"drain", "ditch", "canal", "stream", "river", "stream_end", "waterfall", "rapids", "riverbank"}


def call(path, body):
    req = urllib.request.Request(os.environ["AIRAWAT_API"] + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Run-Token": os.environ["RUN_TOKEN"]}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def unwrap(res):
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
    if AOI_HINT:
        ht = AOI_HINT.split(":")[1] if AOI_HINT.count(":") >= 2 else ""
        if ht and ht != TENANT:
            raise SystemExit(f"FATAL: AOI_ID {AOI_HINT} belongs to '{ht}', not write tenant '{TENANT}'.")
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
    o = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))
    return o, [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), o["id"]


def fetch_waterway_ways(bbox):
    """All `waterway` ways inside the bbox, fetched by TILING (Overpass caps a metro query; the broker caps a
    proxied body at 4 MB). Dedup ways by osm_id across tile seams; each tile retries with backoff."""
    west, south, east, north = bbox
    step = float(os.environ.get("OVERPASS_TILE_DEG", "0.08"))    # waterways are sparser than roads → larger tiles ok
    nx = max(1, int(math.ceil((east - west) / step)))
    ny = max(1, int(math.ceil((north - south) / step)))
    print(f"tiling bbox into {nx}x{ny}={nx*ny} Overpass queries (step {step}°) for waterways", flush=True)
    seen = {}
    done = failed = 0
    for iy in range(ny):
        for ix in range(nx):
            s = south + iy * step; n = min(north, s + step)
            w = west + ix * step; e = min(east, w + step)
            ql = ("[out:json][timeout:120];"
                  f'way["waterway"]({s},{w},{n},{e});'
                  "out tags geom;")
            payload = None
            for attempt in range(4):
                p = unwrap(call("/v1/fetch", {"url": OVERPASS_URL, "method": "POST", "body": ql}))
                if isinstance(p, dict) and isinstance(p.get("elements"), list):
                    payload = p; break
                time.sleep(5 * (attempt + 1))
            done += 1
            if payload is None:
                failed += 1
                print(f"  tile {ix},{iy} failed after retries — skipped (partial, no fabrication)", flush=True)
                continue
            for el in payload["elements"]:
                if el.get("type") == "way":
                    seen[el["id"]] = el
        print(f"  …{done}/{nx*ny} tiles · {len(seen)} unique waterway ways so far", flush=True)
    if failed:
        print(f"  ⚠ {failed}/{nx*ny} tiles skipped after retries — partial drainage coverage (no fabrication)", flush=True)
    return list(seen.values())


def ways_from_pbf(pbf_path, bbox):
    """Extract `waterway` ways inside the bbox from a local OSM PBF via osmium — same shape as the Overpass path."""
    import osmium
    w, s, e, n = bbox
    class _H(osmium.SimpleHandler):
        def __init__(self):
            super().__init__(); self.ways = []; self._c = 0
        def way(self, way):
            if "waterway" not in way.tags:
                return
            coords = []
            try:
                for nd in way.nodes:
                    if nd.location.valid():
                        coords.append({"lon": nd.location.lon, "lat": nd.location.lat})
            except Exception:
                return
            if len(coords) < 2 or not any(w <= c["lon"] <= e and s <= c["lat"] <= n for c in coords):
                return
            self.ways.append({"type": "way", "id": way.id, "tags": dict(way.tags), "geometry": coords})
            self._c += 1
            if self._c % 50000 == 0:
                print(f"  …scanned {self._c} waterway ways so far", flush=True)
    h = _H()
    h.apply_file(pbf_path, locations=True)
    return h.ways


def _clip_to_bbox(src_path, bbox):
    """If the PBF is large and the `osmium` CLI is present, extract a reference-complete slice for the AOI bbox
    (with a small pad) BEFORE the expensive locations=True pass. A regional/zone PBF otherwise forces pyosmium to
    index every node in the country and blows the 180s host-subprocess cap (HTTP 504). osmium extract clips in
    seconds; complete_ways keeps every node of a way crossing the edge so line geometry stays intact. No-op when
    the CLI is missing or the file is small. See ADR-0060 / Wave-1."""
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


def fetch_ways_from_asset(pbf_id, bbox):
    import base64, tempfile
    res = call("/v1/file/get", {"id": pbf_id})
    raw = base64.b64decode(res.get("content_b64") or "")
    if not raw:
        raise SystemExit(f"FATAL: PBF asset {pbf_id} is empty / not in the filestore")
    tf = tempfile.NamedTemporaryFile(suffix=".osm.pbf", delete=False)
    clipped = None
    try:
        tf.write(raw); tf.close()
        print(f"reading waterways from uploaded PBF asset {pbf_id[:20]}… ({len(raw)//1024//1024} MB) via osmium", flush=True)
        path = _clip_to_bbox(tf.name, bbox)
        clipped = path if path != tf.name else None
        return ways_from_pbf(path, bbox)
    finally:
        for p in (tf.name, clipped):
            if p:
                try: os.unlink(p)
                except Exception: pass


def to_feature(el):
    """One OSM waterway way → one geo.hydro_feature LineString; None if it can't become a line or isn't drainage."""
    tags = el.get("tags") or {}
    wtype = tags.get("waterway", "")
    if wtype not in DRAINAGE:                                     # skip non-drainage waterway tags (dam/weir/lock…)
        return None
    geom = el.get("geometry") or []
    coords = [[float(p["lon"]), float(p["lat"])] for p in geom
              if isinstance(p, dict) and "lon" in p and "lat" in p]
    if len(coords) < 2:
        return None
    osm_id = str(el["id"])
    # geo.hydro_feature is a CLOSED schema — only {schema,id,identifiers,tenant,feature,geometry,name,method,
    # source,observed_at} are allowed. The OSM id lives in identifiers[] (via the natural key), NOT a `tags` field.
    return ids.entity(TENANT, "geo/hydro_feature/way", "geo.hydro_feature",
        natural=[("osm_way", osm_id, "OpenStreetMap")],
        extra={
            "tenant": TENANT,
            "feature": wtype,
            "name": tags.get("name"),
            "geometry": {"type": "LineString", "coordinates": coords},
            "method": "osm-overpass",
            "source": {"aoi": AOI_REF, "endpoint": OVERPASS_URL, "via": "ds:airawat:driver/osm-drainage"},
            "observed_at": NOW,
        })


def _haversine_m(a, b):
    (lo1, la1), (lo2, la2) = a, b
    R = 6371000.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dphi, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def main():
    global AOI_REF
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant. Refusing to guess.")
    import h3
    aoi, bbox, res, AOI_REF = resolve_aoi()
    w, s, e, n = bbox
    print(f"tenant={TENANT} · AOI {AOI_REF} · bbox={bbox} · res={res}", flush=True)

    pbf_id = os.environ.get("OSM_PBF_FILE_ID", "").strip()
    if pbf_id:
        ways = fetch_ways_from_asset(pbf_id, bbox)
        print(f"osmium extracted {len(ways)} waterway ways in the AOI (from the uploaded PBF)", flush=True)
    else:
        ways = fetch_waterway_ways(bbox)
        print(f"overpass returned {len(ways)} waterway ways", flush=True)

    written = skipped = 0
    types = {}
    cells = {}                                                   # hc -> {"len": m, "seg": n, "types": {t: m}}
    for el in ways:
        feat = to_feature(el)
        if feat is None:
            skipped += 1
            continue
        call("/v1/write", {"scope": "geo.hydro_feature", "id": feat["id"], "object": feat})
        written += 1
        t = feat["feature"]
        types[t] = types.get(t, 0) + 1
        coords = feat["geometry"]["coordinates"]
        for i in range(len(coords) - 1):
            a, b = coords[i], coords[i + 1]
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            if not (w <= mx <= e and s <= my <= n):
                continue
            hc = h3.latlng_to_cell(my, mx, res)
            L = _haversine_m(a, b)
            d = cells.setdefault(hc, {"len": 0.0, "seg": 0, "types": {}})
            d["len"] += L; d["seg"] += 1; d["types"][t] = d["types"].get(t, 0.0) + L

    if written == 0:
        raise SystemExit("FATAL: no usable waterway ways written for this AOI (Overpass returned no drainage lines).")
    print(f"wrote {written} geo.hydro_feature drainage lines; skipped {skipped} (non-drainage / no geometry)", flush=True)
    print("waterway types:", json.dumps(dict(sorted(types.items())), ensure_ascii=False), flush=True)

    wrote_cells = 0
    for hc, d in cells.items():
        la, lo = h3.cell_to_latlng(hc)
        area = h3.cell_area(hc, unit="km^2")
        prim = max(d["types"], key=d["types"].get) if d["types"] else None
        fact = {
            "schema": "cell.drainage", "id": f"ds:{TENANT}:cell/drainage/{hc}",
            "cell_ref": {"h3_id": hc, "resolution": res, "centroid": [round(lo, 6), round(la, 6)],
                         "boundary": [[round(lo2, 6), round(la2, 6)] for (la2, lo2) in h3.cell_to_boundary(hc)]},
            "drain_length_m": round(d["len"], 1),
            "drain_density_km_per_km2": round((d["len"] / 1000.0) / area, 3) if area else 0.0,
            "segment_count": d["seg"], "primary_type": prim,
            "method": "osm-overpass", "source_ref": SRC, "layer": "L2", "confidence": 0.9,
            "derived_from": ["osm:overpass:waterway"],
            "prov": {"method": "osm-overpass", "confidence": 0.9, "source_ref": SRC,
                     "derived_from": ["osm:overpass:waterway"], "observed_at": NOW},
            "observed_at": NOW}
        fact = {k: v for k, v in fact.items() if v is not None}
        call("/v1/write", {"scope": "cell.drainage", "id": fact["id"], "object": fact,
                           "valid_time": {"from": NOW}, "asserted_at": NOW})
        wrote_cells += 1

    print(f"wrote {wrote_cells} cell.drainage density cells", flush=True)
    print(f"RESULT: {json.dumps({'source': 'osm-overpass', 'geo_hydro_feature': written, 'cell_drainage': wrote_cells})}")


if __name__ == "__main__":
    main()
