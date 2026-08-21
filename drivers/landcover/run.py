#!/usr/bin/env python3
"""landcover driver: the L0→L1 acquisition for the City Scan (ADR-0060).

Confined (>= L1) — its ONLY I/O is the kernel broker:
  read   the space.aoi frame (the bbox to cover)
  fetch  Overpass for landuse/natural/leisure POLYGONS in the bbox (egress-gated to overpass-api.de)
  write  one geo.landcover per polygon: an lc_class (built|water|vegetation|cropland|bare) + native
         GeoJSON geometry (WGS84 [lon,lat], preserved — geometry is truth, ADR-0060) + ODbL source_ref.

If the egress path is unavailable (no secrets service / keyless offline build), it SYNTHESIZES a
deterministic land-cover field over the AOI bbox so the vertical runs end-to-end — clearly labelled. The
output is only as real as its source: run.mode records whether it was 'osm' or 'synthetic'.

Env: AOI_ID (default ds:kmc:space/aoi/kanpur-core) · TENANT (kmc) · OVERPASS_URL · MODE (auto|osm|synthetic)
"""
import json, os, math, urllib.request, urllib.error

AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/kanpur-core")
TENANT = os.environ.get("TENANT", "kmc")
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
MODE = os.environ.get("MODE", "auto")                      # auto: try osm, fall back to synthetic
SOURCE_REF = "ds:airawat:ref/source/osm"
OBS = os.environ.get("OBSERVED_AT", "2025-06-01T00:00:00Z")


def call(path, body):
    req = urllib.request.Request(os.environ["AIRAWAT_API"] + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": os.environ["RUN_TOKEN"]}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def unwrap(res):
    if isinstance(res, dict):
        if "object" in res and isinstance(res["object"], dict):
            return res["object"]
        for k in ("body", "data", "result", "response"):
            if k in res:
                return unwrap(res[k])
        return res
    if isinstance(res, (bytes, str)):
        t = res.decode() if isinstance(res, bytes) else res
        try:
            return json.loads(t)
        except (ValueError, TypeError):
            return t
    return res


def read_aoi():
    o = unwrap(call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}))
    bbox = o.get("bbox") if isinstance(o, dict) else None
    if not bbox or len(bbox) != 4:
        raise SystemExit(f"FATAL: space.aoi {AOI_ID} missing / no 4-el bbox: {o!r}")
    return o, [float(v) for v in bbox]


# ---------------------------------------------------------------- classification (OSM tags → lc_class)
def classify(tags):
    lu, nat, leis = tags.get("landuse"), tags.get("natural"), tags.get("leisure")
    if nat == "water" or tags.get("water") or lu in ("reservoir", "basin") or tags.get("waterway"):
        return "water"
    if lu in ("residential", "commercial", "industrial", "retail", "construction", "garages", "railway") or tags.get("building"):
        return "built"
    if (lu in ("forest", "meadow", "grass", "recreation_ground", "greenfield", "village_green", "cemetery", "orchard", "vineyard")
            or nat in ("wood", "scrub", "grassland", "heath", "wetland") or leis in ("park", "garden", "nature_reserve", "pitch")):
        return "vegetation"
    if lu in ("farmland", "farm", "farmyard", "allotments"):
        return "cropland"
    if nat in ("bare_rock", "sand", "beach", "scree"):
        return "bare"
    return None


def _ring(coords):
    if coords and coords[0] != coords[-1]:
        coords = coords + [coords[0]]
    return coords


def _area_m2(ring):
    """Rough planar area (shoelace) with a lat-scaled metre conversion — enough for coverage stats."""
    if len(ring) < 4:
        return 0.0
    latm = sum(p[1] for p in ring) / len(ring)
    mx = 111320.0 * math.cos(math.radians(latm)); my = 110540.0
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        s += (x1 * mx) * (y2 * my) - (x2 * mx) * (y1 * my)
    return abs(s) / 2.0


def _emit(idx, lc_class, ring, mode):
    ring = _ring([[round(x, 6), round(y, 6)] for x, y in ring])
    fid = f"ds:{TENANT}:geo/landcover/{mode}-{idx}"
    fact = {"schema": "geo.landcover", "id": fid, "lc_class": lc_class,
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "area_m2": round(_area_m2(ring), 1), "aoi_ref": AOI_ID,
            "source_ref": SOURCE_REF, "confidence": 0.9 if mode == "osm" else 0.4,
            "observed_at": OBS, "layer": "L1", "acquired_via": f"ds:airawat:driver/landcover:{mode}"}
    call("/v1/write", {"scope": "geo.landcover", "id": fid, "object": fact,
                       "valid_time": {"from": OBS}, "asserted_at": OBS})
    return lc_class


# ---------------------------------------------------------------- OSM (real) acquisition
def fetch_osm(bbox):
    w, s, e, n = bbox
    ql = ("[out:json][timeout:120];("
          f'way["landuse"]({s},{w},{n},{e});'
          f'way["natural"]({s},{w},{n},{e});'
          f'way["leisure"~"park|garden|nature_reserve"]({s},{w},{n},{e});'
          ");out tags geom;")
    payload = unwrap(call("/v1/fetch", {"url": OVERPASS_URL, "method": "POST", "body": ql}))
    els = payload.get("elements") if isinstance(payload, dict) else None
    if not els:
        raise RuntimeError(f"overpass returned no elements: {str(payload)[:200]}")
    written, classes = 0, {}
    for i, el in enumerate(els):
        if el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        coords = [[float(p["lon"]), float(p["lat"])] for p in geom if isinstance(p, dict) and "lon" in p]
        if len(coords) < 3:
            continue
        lc = classify(el.get("tags") or {})
        if not lc:
            continue
        c = _emit(el["id"], lc, coords, "osm"); classes[c] = classes.get(c, 0) + 1; written += 1
    if written == 0:
        raise RuntimeError("no classifiable OSM landuse polygons in AOI")
    return written, classes


# ---------------------------------------------------------------- synthetic (offline) fallback
def synth(bbox):
    """A deterministic land-cover field over the bbox: a built core, a green belt (N), water (SW), cropland (S)."""
    w, s, e, n = bbox; dw, dh = e - w, n - s
    def box(x0, y0, x1, y1):
        return [[w + x0 * dw, s + y0 * dh], [w + x1 * dw, s + y0 * dh],
                [w + x1 * dw, s + y1 * dh], [w + x0 * dw, s + y1 * dh]]
    parts = [("built", box(0.30, 0.30, 0.72, 0.70)),      # central built core
             ("vegetation", box(0.05, 0.72, 0.95, 0.95)), # green belt across the north
             ("water", box(0.08, 0.10, 0.28, 0.30)),      # water body SW
             ("cropland", box(0.05, 0.02, 0.95, 0.22))]   # cropland strip S
    classes = {}
    for i, (lc, ring) in enumerate(parts):
        c = _emit(i, lc, ring, "synthetic"); classes[c] = classes.get(c, 0) + 1
    return len(parts), classes


def main():
    aoi, bbox = read_aoi()
    print(f"aoi {AOI_ID} ({aoi.get('name','?')}) bbox={bbox} mode={MODE}")
    mode, written, classes = None, 0, {}
    if MODE in ("auto", "osm"):
        try:
            written, classes = fetch_osm(bbox); mode = "osm"
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, Exception) as ex:
            if MODE == "osm":
                raise SystemExit(f"FATAL: OSM acquisition failed and MODE=osm: {ex}")
            print(f"  OSM egress unavailable ({str(ex)[:120]}) → deterministic offline land-cover")
    if mode is None:
        written, classes = synth(bbox); mode = "synthetic"
    print(f"wrote {written} geo.landcover polygons [mode={mode}] classes={json.dumps(dict(sorted(classes.items())))}")
    print(f"RESULT: {json.dumps({'mode': mode, 'written': written, 'source_ref': SOURCE_REF})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
