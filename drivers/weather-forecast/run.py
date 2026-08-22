#!/usr/bin/env python3
"""weather-forecast — per-H3-cell short-range WEATHER FORECAST from Open-Meteo (ADR-0060 L3, operational).

Its ONLY I/O is the kernel broker:
  read   the tenant's space.aoi frame (discovered — never a hardcoded default)
  fetch  the Open-Meteo FORECAST API (free, keyless), egress-gated to api.open-meteo.com
  write  cell.weather_forecast — an hourly `series` of predicted precipitation + temperature per H3 cell

The TEMPORAL / operational sibling of the static climate baseline: it powers the City Scan TIME SCRUBBER (scrub
/ animate the next ~48h over the 2.5D city) and the flood-response loop. Forecasts are low spatial-frequency, so
we sample a coarse GRID, fetch each point's hourly series in ONE call, and assign every H3 cell the series of its
NEAREST sample point. Each cell record carries the whole series + headline peak/total.

REAL Open-Meteo forecast only — fetch or fail loudly. NO synthetic fabrication, NO tenant/AOI default. Each run
REPLACES the tenant's forecast (a forecast is issued fresh; ids are per-cell so the write upserts).

  Env: TENANT · AOI_ID (opt; else discover) · GRID (points/side, default 6) · HOURS (horizon, default 48)
       FORECAST_URL
"""
import datetime as _dt, json, math, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_HINT = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""   # NO default — never guess the tenant
GRID = int(os.environ.get("GRID", "6"))
HOURS = int(os.environ.get("HOURS", "48"))
FORECAST_URL = os.environ.get("FORECAST_URL", "https://api.open-meteo.com/v1/forecast")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())      # issue time (model run) — real, never hardcoded
SRC = "ds:airawat:ref/source/open-meteo"


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def unwrap(res):
    if isinstance(res, dict):
        if "object" in res and isinstance(res["object"], dict):
            return res["object"]
        for k in ("body", "data", "result", "response"):
            if k in res:
                return unwrap(res[k])
    if isinstance(res, (bytes, str)):
        try:
            return json.loads(res.decode() if isinstance(res, bytes) else res)
        except Exception:
            return res
    return res


def resolve_aoi():
    if AOI_HINT:
        ht = AOI_HINT.split(":")[1] if AOI_HINT.count(":") >= 2 else ""
        if ht and ht != TENANT:
            raise SystemExit(f"FATAL: AOI_ID {AOI_HINT} belongs to tenant '{ht}', not write tenant '{TENANT}'.")
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


def grid_points(bbox, k):
    w, s, e, n = bbox
    k = max(2, k)
    return [(s + (n - s) * (i + 0.5) / k, w + (e - w) * (j + 0.5) / k) for i in range(k) for j in range(k)]


def haversine(la1, lo1, la2, lo2):
    r = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _iso(ts):
    if not ts or not isinstance(ts, str):
        return ts
    return ts if ts.endswith("Z") else (ts + ":00Z" if len(ts) == 16 else ts + "Z")


def fetch_forecasts(pts):
    """Hourly precip + temperature forecast series at each sample point, from Open-Meteo. One egress call with
    comma-separated coordinates. Returns [(lat, lon, [ {t,precip_mm,temp_c}, ... ]), ...]. REAL or raise."""
    lats = ",".join(f"{la:.4f}" for la, lo in pts)
    lons = ",".join(f"{lo:.4f}" for la, lo in pts)
    url = (f"{FORECAST_URL}?latitude={lats}&longitude={lons}"
           f"&hourly=precipitation,temperature_2m&forecast_hours={HOURS}&timezone=GMT")
    payload = unwrap(call("/v1/fetch", {"url": url}))
    locs = payload if isinstance(payload, list) else [payload]
    if len(locs) != len(pts) and not (len(locs) == 1 and len(pts) == 1):
        raise RuntimeError(f"forecast returned {len(locs)} locations for {len(pts)} points: {str(payload)[:200]}")
    out = []
    for (la, lo), loc in zip(pts, locs):
        h = (loc or {}).get("hourly") or {}
        times = h.get("time") or []; pr = h.get("precipitation") or []; tp = h.get("temperature_2m") or []
        if not times:
            raise RuntimeError(f"forecast returned no hourly series for point ({la:.3f},{lo:.3f}): {str(loc)[:160]}")
        series = []
        for i, t in enumerate(times[:HOURS]):
            series.append({"t": _iso(t),
                           "precip_mm": round(float(pr[i]), 2) if i < len(pr) and isinstance(pr[i], (int, float)) else None,
                           "temp_c": round(float(tp[i]), 1) if i < len(tp) and isinstance(tp[i], (int, float)) else None})
        out.append((la, lo, series))
    return out


def h3_cells(bbox, res):
    import h3
    w, s, e, n = bbox
    return list(h3.polygon_to_cells(h3.LatLngPoly([(s, w), (s, e), (n, e), (n, w)]), res))


def cell_boundary_lonlat(hcell):
    import h3
    return [[lo, la] for (la, lo) in h3.cell_to_boundary(hcell)]


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant. Refusing to guess.")
    import h3
    aoi, bbox, res, aoi_ref = resolve_aoi()
    pts = grid_points(bbox, GRID)
    print(f"aoi {aoi_ref} bbox={bbox} · sampling {GRID}x{GRID}={len(pts)} Open-Meteo forecast points ({HOURS}h)", flush=True)
    samples = fetch_forecasts(pts)   # REAL or raise
    nsteps = len(samples[0][2])
    print(f"  forecast issued {NOW} · {nsteps} hourly steps · {len(samples)} points", flush=True)

    cells = h3_cells(bbox, res)
    written = 0
    for hc in cells:
        la, lo = h3.cell_to_latlng(hc)
        best = min(samples, key=lambda s: haversine(la, lo, s[0], s[1]))
        series = best[2]
        precs = [st["precip_mm"] for st in series if isinstance(st["precip_mm"], (int, float))]
        fact = {
            "schema": "cell.weather_forecast", "id": f"ds:{TENANT}:cell/weather_forecast/{hc}",
            "cell_ref": {"h3_id": hc, "resolution": res, "centroid": [round(lo, 6), round(la, 6)],
                         "boundary": [[round(x, 6), round(y, 6)] for (x, y) in cell_boundary_lonlat(hc)]},
            "issued_at": NOW, "horizon_h": HOURS, "series": series,
            "peak_precip_mm": round(max(precs), 2) if precs else 0.0,
            "total_precip_mm": round(sum(precs), 1) if precs else 0.0,
            "method": "open-meteo-forecast-nearest", "model": "open-meteo (best_match)", "source_ref": SRC,
            "layer": "L3",
            "prov": {"method": "open-meteo-forecast", "source_ref": SRC, "model": "open-meteo", "issued_at": NOW},
            "observed_at": NOW}
        call("/v1/write", {"scope": "cell.weather_forecast", "id": fact["id"], "object": fact,
                           "valid_time": {"from": NOW}, "asserted_at": NOW})
        written += 1
        if written % 2000 == 0:
            print(f"  …wrote {written}/{len(cells)} cells", flush=True)

    if not written:
        raise SystemExit("FATAL: no cells written (no synthetic fill).")
    print(f"wrote {written} cell.weather_forecast cells ({nsteps}h) from Open-Meteo (real forecast)")
    print(f"RESULT: {json.dumps({'source': 'open-meteo-forecast', 'issued_at': NOW, 'steps': nsteps, 'cells': written})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
