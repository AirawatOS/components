#!/usr/bin/env python3
"""Terrain driver: AOI (context) → H3 cells → a geo.terrain measure per cell (ADR-0053 space block).

Confined — its only I/O is the kernel broker. It READS the area-of-interest (space.aoi) to learn the
bbox + resolution, enumerates the H3 cells covering that bbox, fetches REAL elevation for each cell
centroid from Open-Meteo's elevation API (FREE + KEYLESS, egress-gated to api.open-meteo.com — no secret),
derives slope from the elevation difference to neighbouring cells, and WRITES a geo.terrain fact per cell.
If the egress path is unavailable it falls back to a deterministic SYNTHETIC ridge so the AOI→H3→measure
pipeline still runs keyless; run mode records openmeteo|synthetic.

Env: AOI_ID · MODE (auto|openmeteo|synthetic) · OPEN_METEO_URL
"""
import json, math, os, urllib.request, urllib.error

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/kanpur-core")
OBSERVED_AT = os.environ.get("OBSERVED_AT", "2025-05-12T00:00:00Z")
MODE = os.environ.get("MODE", "auto")
OPEN_METEO_URL = os.environ.get("OPEN_METEO_URL", "https://api.open-meteo.com/v1/elevation")
import h3

def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=20).read())

def cells_in_bbox(bbox, res):
    """West,south,east,north → the set of res-cells covering it. polygon_to_cells where available,
    else a centroid grid-scan (robust across h3 builds)."""
    w, s, e, n = bbox
    try:
        poly = h3.LatLngPoly([(s, w), (s, e), (n, e), (n, w)])
        cells = set(h3.polygon_to_cells(poly, res))
        if cells:
            return sorted(cells)
    except Exception:
        pass
    step = h3.average_hexagon_edge_length(res, unit="km") / 111.0 / 2.0   # ~half a cell in degrees
    cells = set()
    lat = s
    while lat <= n:
        lng = w
        while lng <= e:
            cells.add(h3.latlng_to_cell(lat, lng, res)); lng += step
        lat += step
    return sorted(cells)

def classify(slope):
    return ("flat" if slope < 2 else "gently_sloping" if slope < 5 else
            "moderate_slope" if slope < 15 else "steep" if slope < 30 else "very_steep")

def fetch_elevations(cells):
    """REAL elevation per cell centroid from Open-Meteo (keyless, one egress call; batches of 100)."""
    elev = {}
    for i in range(0, len(cells), 100):
        chunk = cells[i:i + 100]
        pts = [h3.cell_to_latlng(h) for h in chunk]
        url = (OPEN_METEO_URL + "?latitude=" + ",".join(f"{p[0]:.5f}" for p in pts)
               + "&longitude=" + ",".join(f"{p[1]:.5f}" for p in pts))
        payload = _call("/v1/fetch", {"url": url})
        body = payload.get("body") if isinstance(payload, dict) else None
        data = json.loads(body) if isinstance(body, str) else (payload if isinstance(payload, dict) else {})
        arr = data.get("elevation")
        if not isinstance(arr, list) or len(arr) != len(chunk):
            raise RuntimeError(f"open-meteo elevation shape unexpected: {str(data)[:160]}")
        for h, e in zip(chunk, arr):
            if e is not None:
                elev[h] = float(e)
    if not elev:
        raise RuntimeError("no elevations returned")
    return elev

def slope_deg(h, elev, res):
    """Slope from the elevation drop to ring-1 neighbours over the ~cell-centre distance."""
    try:
        nbrs = [n for n in h3.grid_disk(h, 1) if n != h and n in elev]
    except Exception:
        nbrs = []
    if h not in elev or not nbrs:
        return 0.0
    dist = h3.average_hexagon_edge_length(res, unit="km") * 1000.0 * 1.732   # adjacent-centre distance (m)
    rise = max(abs(elev[h] - elev[n]) for n in nbrs)
    return round(math.degrees(math.atan2(rise, dist)), 2)

def main():
    ctx = json.loads(urllib.request.urlopen(urllib.request.Request(
        API + "/v1/context", headers={"X-Run-Token": TOKEN})).read())
    print(f"driver run: {ctx['component_id']}  caps={[c['verb']+':'+c['scope'] for c in ctx['effective_capabilities']]}")

    aoi = _call("/v1/read", {"scope": "space.aoi", "id": AOI_ID})["object"]
    if not aoi:
        print(f"no AOI at {AOI_ID}"); return 1
    bbox, res = aoi["bbox"], aoi["h3_res"]
    cells = cells_in_bbox(bbox, res)
    print(f"AOI '{aoi['name']}' bbox={bbox} res={res} → {len(cells)} H3 cells (mode={MODE})")

    # REAL elevation only — fetch or fail loudly (no synthetic fabrication).
    try:
        elev_map = fetch_elevations(cells)
    except Exception as ex:
        raise SystemExit(f"FATAL: real elevation acquisition failed — writing nothing (no synthetic fallback): {ex}")
    print(f"  fetched {len(elev_map)} real elevations from Open-Meteo")

    written = 0
    for h in cells:
        if h not in elev_map:
            continue                                    # only real observations; skip cells with no elevation
        lat, lng = h3.cell_to_latlng(h)
        boundary = [[round(lo, 6), round(la, 6)] for (la, lo) in h3.cell_to_boundary(h)]  # [lon,lat] ring
        elev = round(elev_map[h], 1); slope = slope_deg(h, elev_map, res)
        fact = {
            "schema": "geo.terrain", "id": f"ds:kmc:geo/terrain/{h}",
            "space": {"h3": h, "res": res, "centroid": [round(lng, 6), round(lat, 6)], "boundary": boundary},
            "elevation_m": elev, "slope_deg": slope, "terrain_class": classify(slope), "unit": "m",
            "observed_at": OBSERVED_AT, "source_ref": "ds:airawat:ref/source/open-meteo",
            "prov": {"method": "ds:airawat:method/open-meteo-elevation", "component": ctx["component_id"]},
        }
        _call("/v1/write", {"scope": "geo.terrain", "id": fact["id"], "object": fact,
                            "valid_time": {"from": OBSERVED_AT}, "asserted_at": OBSERVED_AT})
        written += 1
    print(f"wrote {written} geo.terrain cells (real Open-Meteo elevation)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
