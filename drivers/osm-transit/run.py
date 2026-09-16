#!/usr/bin/env python3
"""osm-transit driver — the PUBLIC-TRANSPORT network for an AOI from OpenStreetMap/Overpass (ADR-0060 L1).

Emits L1 native geometry into the existing governed `geo.osm_feature` schema — no new noun — distinguished by
the `feature` field (as roads use the `highway` class):
  metro/rail LINES   : ways  railway=subway|light_rail|rail|monorail|tram          → LineString
  stations / halts   : nodes railway=station|halt  OR  station=subway              → Point
  bus stops          : nodes highway=bus_stop  OR  public_transport=platform(bus)  → Point

Its ONLY I/O is the kernel broker: read the tenant's space.aoi frame · fetch Overpass (egress-gated) · write
geo.osm_feature in batches. Deterministic natural-key id (ds:<tenant>:geo/osm_feature/<way|node>/<osm_id>) so
re-runs upsert. Real OSM only — fails loudly if the AOI is missing or Overpass returns nothing. No AOI default.

`python run.py --selftest` hits Overpass directly for a small central-Delhi bbox (no broker) to validate the
query + parser against real data.

Inputs (env): AOI_ID (optional) · TENANT · OVERPASS_URL
"""
import json, math, os, shutil, subprocess, sys, time, urllib.request
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_common"))

AOI_HINT = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
AOI_REF = ""
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/osm"

# The one Overpass query per tile — ways (lines) + nodes (stations, bus stops) in one union.
RAIL_WAY = "^(subway|light_rail|rail|monorail|tram)$"
def _tile_ql(s, w, n, e):
    return ("[out:json][timeout:120];(" +
            f'way["railway"~"{RAIL_WAY}"]({s},{w},{n},{e});' +
            f'node["railway"~"^(station|halt)$"]({s},{w},{n},{e});' +
            f'node["station"="subway"]({s},{w},{n},{e});' +
            f'node["highway"="bus_stop"]({s},{w},{n},{e});' +
            f'node["public_transport"="platform"]["bus"="yes"]({s},{w},{n},{e});' +
            ");out tags geom;")


def call(path, body):
    req = urllib.request.Request(os.environ["AIRAWAT_API"] + path,
        data=json.dumps(body).encode("utf-8"),
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
        try: return json.loads(text)
        except (ValueError, TypeError): return text
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
        raise SystemExit(f"FATAL: no space.aoi for tenant '{TENANT}' — run City Setup first.")
    o = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))
    return o, [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), o["id"]


def _overpass(ql, direct=False):
    """Fetch one Overpass query — via the broker in a real run, or DIRECT for --selftest."""
    if direct:
        req = urllib.request.Request(OVERPASS_URL, data=("data=" + ql).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "AirawatOS/osm-transit (+https://airawat.org; manishsv@gmail.com)"}, method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode("utf-8"))
    return unwrap(call("/v1/fetch", {"url": OVERPASS_URL, "method": "POST", "body": ql}))


def fetch_transit(bbox, direct=False):
    """All transit ways + nodes in the bbox, fetched by TILING (broker caps a proxied body at 4 MB). Dedup
    elements by (type,id) across tile seams; each tile retries with backoff (Overpass throttles)."""
    west, south, east, north = bbox
    step = float(os.environ.get("OVERPASS_TILE_DEG", "0.06"))
    nx = max(1, int(math.ceil((east - west) / step)))
    ny = max(1, int(math.ceil((north - south) / step)))
    print(f"tiling bbox into {nx}x{ny}={nx*ny} Overpass queries (step {step}°)", flush=True)
    seen = {}; done = failed = 0
    for iy in range(ny):
        for ix in range(nx):
            s = south + iy * step; n = min(north, s + step)
            w = west + ix * step; e = min(east, w + step)
            ql = _tile_ql(s, w, n, e); payload = None
            for attempt in range(5):
                try:
                    p = _overpass(ql, direct=direct)
                except Exception as ex:                          # Overpass 429/504/timeout → back off and retry
                    p = None; print(f"  tile {ix},{iy} attempt {attempt+1}: {type(ex).__name__} {str(ex)[:60]}", flush=True)
                if isinstance(p, dict) and isinstance(p.get("elements"), list):
                    payload = p; break
                time.sleep(6 * (attempt + 1))
            done += 1
            if payload is None:
                failed += 1; print(f"  tile {ix},{iy} failed after retries — skipped", flush=True); continue
            for el in payload["elements"]:
                if el.get("type") in ("way", "node"):
                    seen[(el["type"], el["id"])] = el
        print(f"  …{done}/{nx*ny} tiles · {len(seen)} unique elements so far", flush=True)
    if not seen:
        raise SystemExit("FATAL: Overpass returned no transit elements for any tile")
    if failed:
        print(f"  ⚠ {failed}/{nx*ny} tiles skipped after retries — partial coverage (no fabrication)", flush=True)
    return list(seen.values())


_RAILW = {"subway", "light_rail", "rail", "monorail", "tram"}


def _clip_to_bbox(src_path, bbox):
    """osmium-extract a reference-complete AOI slice from a large regional PBF BEFORE the locations=True pass
    (else pyosmium indexes the whole country and blows the host subprocess cap). No-op if CLI missing / small."""
    try:
        if not shutil.which("osmium") or os.path.getsize(src_path) < 150 * 1024 * 1024:
            return src_path
    except Exception:
        return src_path
    w, s, e, n = bbox; pad = 0.03; dst = src_path + ".clip.osm.pbf"
    try:
        t0 = time.time()
        subprocess.run(["osmium", "extract", "-b", f"{w-pad},{s-pad},{e+pad},{n+pad}", src_path,
                        "-o", dst, "--overwrite", "-s", "complete_ways"], check=True, capture_output=True, timeout=150)
        print(f"  clipped PBF to AOI → {os.path.getsize(dst)//1024//1024} MB in {time.time()-t0:.0f}s", flush=True)
        return dst
    except Exception as ex:
        print(f"  osmium clip skipped ({str(ex)[:80]}) — reading full PBF", flush=True); return src_path


def transit_from_pbf(pbf_path, bbox):
    """Extract transit ELEMENTS (railway ways + station/bus-stop nodes) inside the bbox from a local OSM PBF via
    pyosmium — SAME element shape as the Overpass path so to_feature() is unchanged. The reliable, throttle-free
    source: the tenant's regional PBF (reused from City Setup) read locally."""
    import osmium
    w, s, e, n = bbox; els = []
    class _H(osmium.SimpleHandler):
        def __init__(self): super().__init__(); self._c = 0
        def way(self, way):
            if way.tags.get("railway") not in _RAILW:
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
            els.append({"type": "way", "id": way.id, "tags": dict(way.tags), "geometry": coords})
            self._c += 1
        def node(self, node):
            t = node.tags
            is_station = (t.get("railway") in ("station", "halt")) or (t.get("station") == "subway")
            is_bus = (t.get("highway") == "bus_stop") or (t.get("public_transport") == "platform" and t.get("bus") == "yes")
            if not (is_station or is_bus):
                return
            loc = node.location
            if not loc.valid() or not (w <= loc.lon <= e and s <= loc.lat <= n):
                return
            els.append({"type": "node", "id": node.id, "lon": loc.lon, "lat": loc.lat, "tags": dict(t)})
    _H().apply_file(pbf_path, locations=True)
    if not els:
        raise SystemExit("FATAL: no transit ways/nodes in the PBF for this AOI")
    return els


def fetch_transit_from_asset(pbf_id, bbox):
    """Pull the uploaded PBF asset from the filestore (via the broker), osmium-extract, read transit elements."""
    import base64, tempfile
    raw = base64.b64decode((call("/v1/file/get", {"id": pbf_id}) or {}).get("content_b64") or "")
    if not raw:
        raise SystemExit(f"FATAL: PBF asset {pbf_id} is empty / not in the filestore")
    tf = tempfile.NamedTemporaryFile(suffix=".osm.pbf", delete=False); clipped = None
    try:
        tf.write(raw); tf.close()
        print(f"reading transit from uploaded PBF {pbf_id[:20]}… ({len(raw)//1024//1024} MB) via osmium", flush=True)
        path = _clip_to_bbox(tf.name, bbox); clipped = path if path != tf.name else None
        return transit_from_pbf(path, bbox)
    finally:
        for p in (tf.name, clipped):
            if p:
                try: os.unlink(p)
                except Exception: pass


def _mode(tags):
    """Classify the transit mode for quick filtering/rendering (kept inside tags-derived `feature`)."""
    rw = tags.get("railway")
    if rw in ("subway",): return "metro"
    if rw in ("light_rail", "monorail"): return "metro"
    if rw in ("rail",): return "rail"
    if rw in ("tram",): return "tram"
    if rw in ("station", "halt") or tags.get("station") == "subway": return "station"
    if tags.get("highway") == "bus_stop" or tags.get("public_transport") == "platform": return "bus"
    return "transit"


def to_feature(el):
    """One OSM transit element → one geo.osm_feature (LineString for ways, Point for nodes)."""
    import ids
    tags = el.get("tags") or {}
    if el.get("type") == "way":
        geom = el.get("geometry") or []
        coords = [[float(p["lon"]), float(p["lat"])] for p in geom
                  if isinstance(p, dict) and "lon" in p and "lat" in p]
        if len(coords) < 2:
            return None
        geometry = {"type": "LineString", "coordinates": coords}
        feature = tags.get("railway", "rail"); path = "geo/osm_feature/way"; otype = "way"; nkey = "osm_way"
    else:  # node
        if "lon" not in el or "lat" not in el:
            return None
        geometry = {"type": "Point", "coordinates": [float(el["lon"]), float(el["lat"])]}
        feature = tags.get("railway") or ("bus_stop" if tags.get("highway") == "bus_stop" else
                  ("platform" if tags.get("public_transport") == "platform" else "station"))
        path = "geo/osm_feature/node"; otype = "node"; nkey = "osm_node"
    osm_id = str(el["id"])
    # stash the coarse mode INSIDE tags (allowed free-form) so consumers can filter metro/rail/tram/station/bus
    tags = dict(tags); tags["_transit_mode"] = _mode(tags)
    return ids.entity(TENANT, path, "geo.osm_feature",
        natural=[(nkey, osm_id, "OpenStreetMap")],
        extra={"tenant": TENANT, "osm_id": osm_id, "osm_type": otype, "feature": feature,
               "tags": tags, "geometry": geometry,
               "source": {"aoi": AOI_REF, "endpoint": OVERPASS_URL, "via": "ds:airawat:driver/osm-transit"}})


def main():
    global AOI_REF
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant.")
    aoi, bbox, res, AOI_REF = resolve_aoi()
    print(f"tenant={TENANT} · AOI {AOI_REF} · bbox={bbox} · res={res}", flush=True)
    # SOURCE: the uploaded regional OSM PBF (reliable, throttle-free — reused from City Setup) if the tenant
    # provided one; else live Overpass (rate-limited at metro scale).
    pbf_id = os.environ.get("OSM_PBF_FILE_ID", "").strip()
    if pbf_id:
        els = fetch_transit_from_asset(pbf_id, bbox)
        print(f"osmium extracted {len(els)} transit elements from the uploaded PBF", flush=True)
    else:
        els = fetch_transit(bbox)
        print(f"overpass returned {len(els)} transit elements", flush=True)
    written = skipped = 0; classes = {}
    _batch = []
    def _flush():
        nonlocal written
        if not _batch: return
        r = call("/v1/write/batch", {"items": _batch, "asserted_at": NOW}) or {}
        written += int(r.get("written", 0))
        if r.get("errors"): print("  batch sample errors:", json.dumps(r["errors"][:2])[:300], flush=True)
        _batch.clear()
    for el in els:
        feat = to_feature(el)
        if feat is None:
            skipped += 1; continue
        _batch.append({"scope": "geo.osm_feature", "id": feat["id"], "object": feat})
        m = feat["tags"]["_transit_mode"]; classes[m] = classes.get(m, 0) + 1
        if len(_batch) >= 2000: _flush()
    _flush()
    if written == 0:
        raise SystemExit("FATAL: no usable transit features found for this AOI")
    print(f"wrote {written} geo.osm_feature transit features; skipped {skipped}", flush=True)
    print("by mode:", json.dumps(dict(sorted(classes.items())), ensure_ascii=False), flush=True)
    print(f"RESULT: {json.dumps({'source': 'osm-overpass', 'geo_osm_feature': written, 'by_mode': classes})}")


def selftest():
    """Hit Overpass DIRECTLY for a small central-Delhi bbox — validates the query + parser on real data."""
    global TENANT, AOI_REF
    TENANT = TENANT or "selftest"; AOI_REF = "selftest:delhi-central"
    bbox = [77.20, 28.58, 77.26, 28.64]   # ~Connaught Place → central Delhi (small; ~1 tile)
    print("SELFTEST: direct Overpass, central-Delhi bbox", bbox, flush=True)
    els = fetch_transit(bbox, direct=True)
    feats = [f for f in (to_feature(e) for e in els) if f]
    modes = {}; ways = nodes = 0
    for f in feats:
        modes[f["tags"]["_transit_mode"]] = modes.get(f["tags"]["_transit_mode"], 0) + 1
        ways += f["osm_type"] == "way"; nodes += f["osm_type"] == "node"
    print(f"  elements={len(els)} → features={len(feats)} (ways={ways}, nodes={nodes})", flush=True)
    print("  by mode:", json.dumps(dict(sorted(modes.items())), ensure_ascii=False), flush=True)
    # show a couple of real named examples
    named = [(f["feature"], (f["tags"].get("name") or "")[:40]) for f in feats if f["tags"].get("name")][:8]
    print("  samples:", json.dumps(named, ensure_ascii=False), flush=True)
    print("SELFTEST PASS" if feats else "SELFTEST FAIL (no features)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
