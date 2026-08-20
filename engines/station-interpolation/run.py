#!/usr/bin/env python3
"""station-interpolation engine: air.sensor_reading (station POINTS) → IDW → air.quality (cell SURFACE).

Confined — its only I/O is the kernel broker. It READS the AOI (space.aoi) for the bbox + H3 resolution,
QUERIES every ground-station reading in the tenant (air.sensor_reading), enumerates the H3 cells covering
the AOI, and for each cell interpolates the station values by INVERSE-DISTANCE WEIGHTING (weight = 1/dᵖ).
It writes one air.quality per cell with method="idw" and a distance-based confidence (nearer a station →
higher). It does NOT fabricate observations: with zero stations carrying PM2.5 it writes nothing and says so.
The output is only as real as its inputs — interpolating demo readings yields demo cells.
"""
import json, os, math, urllib.request
import h3

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/kanpur-core")
WRITE_TENANT = os.environ.get("WRITE_TENANT", "kmc")
POWER = float(os.environ.get("IDW_POWER", "2"))            # distance exponent p in 1/dᵖ
_PARAMS = ("pm25", "no2", "o3")


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def cells_in_bbox(bbox, res):
    """West,south,east,north → the res-cells covering it. polygon_to_cells where available, else a
    centroid grid-scan (robust across h3 builds)."""
    w, s, e, n = bbox
    try:
        poly = h3.LatLngPoly([(s, w), (s, e), (n, e), (n, w)])
        return set(h3.polygon_to_cells(poly, res))
    except Exception:
        cells = set()
        step = h3.average_hexagon_edge_length(res, unit="km") / 111.0 / 2.0
        lat = s
        while lat <= n:
            lng = w
            while lng <= e:
                cells.add(h3.latlng_to_cell(lat, lng, res)); lng += step
            lat += step
        return cells


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def main():
    aoi = _call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object")
    if not aoi:
        print(f"FAIL: no AOI at {AOI_ID}"); return 1
    bbox, res = aoi["bbox"], aoi["h3_res"]

    rows = _call("/v1/query", {"scope": "air.sensor_reading", "limit": 5000}).get("results", [])
    stations = []                                          # (lat, lng, {param: value}, observed_at)
    for r in rows:
        o = r.get("object") or r
        sp = o.get("space") or {}; c = sp.get("centroid")
        if not c or o.get("pm25") is None:                 # need a location and at least PM2.5
            continue
        vals = {p: o[p] for p in _PARAMS if o.get(p) is not None}
        stations.append((c[1], c[0], vals, o.get("observed_at") or ""))   # centroid is [lng, lat]
    if not stations:
        print(f"no air.sensor_reading with PM2.5 in {WRITE_TENANT} — nothing to interpolate. "
              f"Run the air-sensors driver first (needs a valid OPENAQ_API_KEY). Nothing written.")
        return 0

    obs = max(s[3] for s in stations) or "1970-01-01T00:00:00Z"
    cells = cells_in_bbox(bbox, res)
    print(f"AOI '{aoi.get('name')}' bbox={bbox} res={res} → {len(cells)} cells; "
          f"{len(stations)} stations; IDW p={POWER}")

    written = 0
    for hcell in cells:
        lat, lng = h3.cell_to_latlng(hcell)
        acc = {p: [0.0, 0.0] for p in _PARAMS}             # param -> [Σ w·v, Σ w]
        nearest = float("inf")
        for slat, slng, vals, _ in stations:
            d = _haversine_km(lat, lng, slat, slng)
            nearest = min(nearest, d)
            w = 1.0 / (max(d, 0.05) ** POWER)              # cap tiny d so an on-station cell isn't infinite
            for p, v in vals.items():
                acc[p][0] += w * v; acc[p][1] += w
        out = {p: round(sw / w, 2) for p, (sw, w) in acc.items() if w > 0}
        if "pm25" not in out:
            continue
        confidence = round(1.0 / (1.0 + nearest / 5.0), 3)  # within ~5 km of a station ⇒ ~0.5+, decays with distance
        boundary = [[round(lo, 6), round(la, 6)] for (la, lo) in h3.cell_to_boundary(hcell)]  # [lon,lat] ring for map render
        rid = f"ds:{WRITE_TENANT}:air/quality/{hcell}-{obs[:13]}"
        fact = {"schema": "air.quality", "id": rid,
                "space": {"h3": hcell, "res": res, "centroid": [round(lng, 5), round(lat, 5)], "boundary": boundary},
                **out, "observed_at": obs, "method": "idw", "confidence": confidence}
        _call("/v1/write", {"scope": "air.quality", "id": rid, "object": fact,
                            "valid_time": {"from": obs}, "asserted_at": obs})
        written += 1

    print(f"interpolated {written} air.quality cells from {len(stations)} stations (IDW p={POWER}, obs {obs})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
