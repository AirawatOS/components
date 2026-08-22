#!/usr/bin/env python3
"""Terrain driver: AOI (context) → H3 cells → a geo.terrain measure per cell (ADR-0053 space block).

Confined — its only I/O is the kernel broker. It READS the area-of-interest (space.aoi) to learn the
bbox + resolution, enumerates the H3 cells covering that bbox, fetches REAL elevation for each cell
centroid from Open-Meteo's elevation API (FREE + KEYLESS, egress-gated to api.open-meteo.com — no secret),
derives slope from the elevation difference to neighbouring cells, and WRITES a geo.terrain fact per cell.
REAL elevation ONLY — if the egress path is unavailable the driver writes NOTHING and fails loudly. No
synthetic fallback. (Superseded by terrain-dem, which reads Copernicus GLO-30 via GEE.)

Env: AOI_ID · TENANT · OPEN_METEO_URL
"""
import json, math, os, time, urllib.request, urllib.error

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""   # NO default — never guess the tenant
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())   # real acquisition time — never a hardcoded date
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
    """REAL elevation per cell centroid from Open-Meteo (keyless, batches of 100). Metro AOIs need many
    batches, so this PACES under the broker's egress rate limit and RETRIES a rate-limited/transient batch
    with backoff. A batch that never succeeds is SKIPPED (partial REAL data — never a synthetic fill); we
    only fail if nothing at all came back."""
    import time as _t
    elev = {}; failed = 0; n = (len(cells) + 99) // 100
    for i in range(0, len(cells), 100):
        chunk = cells[i:i + 100]
        pts = [h3.cell_to_latlng(h) for h in chunk]
        url = (OPEN_METEO_URL + "?latitude=" + ",".join(f"{p[0]:.5f}" for p in pts)
               + "&longitude=" + ",".join(f"{p[1]:.5f}" for p in pts))
        arr = None
        for attempt in range(4):
            try:
                payload = _call("/v1/fetch", {"url": url})
            except Exception:
                _t.sleep(2.5 * (attempt + 1)); continue      # broker 429/HTTPError → back off + retry
            body = payload.get("body") if isinstance(payload, dict) else None
            try:
                data = json.loads(body) if isinstance(body, str) else (payload if isinstance(payload, dict) else {})
            except Exception:
                data = {}
            cand = data.get("elevation")
            if isinstance(cand, list) and len(cand) == len(chunk):
                arr = cand; break
            _t.sleep(2.5 * (attempt + 1))                    # upstream rate-limit / error body → back off + retry
        if arr is None:
            failed += 1; continue                            # skip this batch — partial real, no fabrication
        for h, e in zip(chunk, arr):
            if e is not None:
                elev[h] = float(e)
        _t.sleep(2.1)                                        # pace ~28/min, under the declared 30/min egress limit
    if not elev:
        raise RuntimeError("no elevations returned (every batch rate-limited/failed)")
    if failed:
        print(f"  ⚠ {failed}/{n} elevation batches failed after retries — wrote {len(elev)} REAL cells (partial, no fill)")
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
    print(f"AOI '{aoi['name']}' bbox={bbox} res={res} → {len(cells)} H3 cells")

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
            "schema": "geo.terrain", "id": f"ds:{TENANT}:geo/terrain/{h}",
            "space": {"h3": h, "res": res, "centroid": [round(lng, 6), round(lat, 6)], "boundary": boundary},
            "elevation_m": elev, "slope_deg": slope, "terrain_class": classify(slope), "unit": "m",
            "observed_at": NOW, "source_ref": "ds:airawat:ref/source/open-meteo",
            "prov": {"method": "ds:airawat:method/open-meteo-elevation", "component": ctx["component_id"],
                     "observed_at": NOW},
        }
        _call("/v1/write", {"scope": "geo.terrain", "id": fact["id"], "object": fact,
                            "valid_time": {"from": NOW}, "asserted_at": NOW})
        written += 1
    print(f"wrote {written} geo.terrain cells (real Open-Meteo elevation)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
