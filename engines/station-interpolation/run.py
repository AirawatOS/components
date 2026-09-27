#!/usr/bin/env python3
"""station-interpolation engine (aos SDK): air.sensor_reading (station POINTS) → IDW → air.quality (cell SURFACE).

Confined — its only I/O is the SDK's ctx (broker transport). It READS the AOI (space.aoi) for the bbox + H3
resolution, QUERIES every ground-station reading in the tenant (air.sensor_reading), enumerates the H3 cells
covering the AOI, and for each cell interpolates the station values by INVERSE-DISTANCE WEIGHTING (weight =
1/dᵖ). It writes one air.quality per cell with method="idw" and a distance-based confidence (nearer a station →
higher). It does NOT fabricate observations: with zero stations carrying PM2.5 it writes nothing and says so.
The output is only as real as its inputs — interpolating demo readings yields demo cells.

aos SDK: tenant resolution + registry read/query/write (with bitemporal valid_from) are the SDK's ctx. Each
air.quality cell is written as a bitemporal ASSERTION valid from the observation time (ctx.write_many
valid_from_field), one batched round-trip.
"""
import os, math
from aos import driver, ctx

_PARAMS = ("pm25", "no2", "o3")                            # an interpolator must never trust raw low-cost points blindly


def cells_in_bbox(h3, bbox, res):
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


@driver.main
def run():
    import h3
    AOI_ID = os.environ.get("AOI_ID", "")      # explicit AOI, else discovered for THIS run's tenant — NEVER a hardcoded city
    POWER = float(os.environ.get("IDW_POWER", "2"))            # distance exponent p in 1/dᵖ
    PM_MAX = float(os.environ.get("PM_MAX_PLAUSIBLE", "985"))  # drop implausible station points (offline 0 / sentinel 999)

    T = ctx.tenant
    if not T:
        print("FAIL: could not resolve the run tenant (/v1/context)."); return 1
    if AOI_ID:
        aoi = ctx.get(AOI_ID, "space.aoi")
    else:                                                  # discover the tenant's largest AOI (no hardcoded city)
        mine = [(r.get("object") or r) for r in ctx.facts("space.aoi").list()
                if str((r.get("object") or r).get("id", "")).startswith(f"ds:{T}:")
                and len((r.get("object") or r).get("bbox") or []) == 4]
        aoi = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1]))) if mine else None
    if not aoi:
        print(f"FAIL: no space.aoi for tenant '{T}' — set AOI_ID or run City Setup first."); return 1
    bbox, res = aoi["bbox"], aoi["h3_res"]

    rows = ctx.facts("air.sensor_reading").list()
    latest, dropped = {}, 0                                # sensor key -> (lat, lng, {param:value}, observed_at)
    for r in rows:
        o = r.get("object") or r
        sp = o.get("space") or {}; c = sp.get("centroid"); pm = o.get("pm25")
        if not c or pm is None:                            # need a location and at least PM2.5
            continue
        if pm <= 0 or pm >= PM_MAX:                        # QC: skip device faults so they never seed a false hotspot
            dropped += 1; continue
        key = o.get("sensor_id") or (round(c[0], 5), round(c[1], 5))   # ONE reading per station: the most recent
        obs = o.get("observed_at") or ""
        if key not in latest or obs > latest[key][3]:      # keep the latest — stale readings must not dilute the surface
            vals = {p: o[p] for p in _PARAMS if o.get(p) is not None and (p != "pm25" or 0 < o[p] < PM_MAX)}
            latest[key] = (c[1], c[0], vals, obs)          # centroid is [lng, lat]
    stations = list(latest.values())
    if not stations:
        print(f"no plausible air.sensor_reading with PM2.5 in '{T}' ({dropped} dropped as faulty) — nothing to "
              f"interpolate. Run an air driver first. Nothing written.")
        return 0

    obs = max(s[3] for s in stations) or "1970-01-01T00:00:00Z"
    cells = cells_in_bbox(h3, bbox, res)
    print(f"AOI '{aoi.get('name')}' bbox={bbox} res={res} → {len(cells)} cells; "
          f"{len(stations)} stations; IDW p={POWER}")

    facts = []
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
        rid = f"ds:{T}:air/quality/{hcell}-{obs[:13]}"
        fact = {"schema": "air.quality", "id": rid,
                "space": {"h3": hcell, "res": res, "centroid": [round(lng, 5), round(lat, 5)], "boundary": boundary},
                **out, "observed_at": obs, "method": "idw", "confidence": confidence,
                # prov: the measure's derivation block the house rule requires (ADR-0041/0063 R5).
                "prov": {"method": "idw", "confidence": confidence, "derived_from": ["air.sensor_reading@stations"],
                         "observed_at": obs}}
        facts.append(fact)

    # bitemporal: each cell's validity pinned to the observation time; one batched round-trip
    written = ctx.write_many(facts, valid_from_field="observed_at", asserted_at=obs) if facts else 0
    print(f"interpolated {written} air.quality cells from {len(stations)} stations (IDW p={POWER}, obs {obs})")
    return 0


if __name__ == "__main__":
    raise SystemExit(driver.run())
