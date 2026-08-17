#!/usr/bin/env python3
"""Terrain driver: AOI (context) → H3 cells → a geo.terrain measure per cell (ADR-0053 space block).

Confined — its only I/O is the kernel broker. It READS the area-of-interest (space.aoi) to learn the
bbox + resolution, enumerates the H3 cells covering that bbox, derives terrain for each, and WRITES a
geo.terrain fact per cell. Elevation here is a deterministic SYNTHETIC surface (a ridge) computed from
the cell centroid — real DEM ingestion (SRTM/Copernicus/Open-Elevation) is a driver-internal swap that
needs a brokered egress or a bundled DEM; the AOI→H3→measure pipeline is what this proves.
"""
import json, math, os, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/kanpur-core")
OBSERVED_AT = os.environ.get("OBSERVED_AT", "2025-05-12T00:00:00Z")
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

def synthetic_terrain(lat, lng, bbox):
    """A smooth deterministic 'ridge' over the AOI so cells vary believably (offline, no data source)."""
    w, s, e, n = bbox
    u = (lng - w) / max(e - w, 1e-9); v = (lat - s) / max(n - s, 1e-9)
    elev = 110.0 + 40.0 * math.sin(math.pi * u) * math.cos(0.5 * math.pi * v) + 8.0 * v
    # slope from the analytic gradient magnitude (m per ~cell), then a coarse degree estimate
    dED = 40.0 * math.pi * math.cos(math.pi * u) * math.cos(0.5 * math.pi * v)   # d/du
    slope = min(45.0, abs(dED) / 700.0 * 90.0)
    return round(elev, 1), round(slope, 2)

def classify(slope):
    return ("flat" if slope < 2 else "gently_sloping" if slope < 5 else
            "moderate_slope" if slope < 15 else "steep" if slope < 30 else "very_steep")

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

    for h in cells:
        lat, lng = h3.cell_to_latlng(h)
        elev, slope = synthetic_terrain(lat, lng, bbox)
        fact = {
            "schema": "geo.terrain", "id": f"ds:kmc:geo/terrain/{h}",
            "space": {"h3": h, "res": res, "centroid": [round(lng, 6), round(lat, 6)]},
            "elevation_m": elev, "slope_deg": slope, "terrain_class": classify(slope), "unit": "m",
            "observed_at": OBSERVED_AT,
            "prov": {"method": "ds:kmc:method/synthetic-dem", "component": ctx["component_id"]},
        }
        _call("/v1/write", {"scope": "geo.terrain", "id": fact["id"], "object": fact,
                            "valid_time": {"from": OBSERVED_AT}, "asserted_at": OBSERVED_AT})
    print(f"wrote {len(cells)} geo.terrain cells")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
