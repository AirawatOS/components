#!/usr/bin/env python3
"""era5-pblh — PLANETARY BOUNDARY-LAYER HEIGHT from ECMWF (Open-Meteo forecast API) (ADR-0073 M2).

The planetary boundary-layer height (PBLH, the daytime mixing depth) is the single strongest meteorological
control on surface PM2.5 (brief §23.7): a shallow nocturnal/winter PBL confines emissions to a thin layer and
concentrations spike; a deep, well-mixed daytime PBL dilutes the same emissions. Any credible local PM2.5 model
must carry it. We source the ECMWF `boundary_layer_height` KEYLESS via the Open-Meteo forecast API, so this is a
host subprocess like rainfall-openmeteo / weather-forecast.

Its ONLY I/O is the kernel broker:
  read   the tenant's space.aoi frame (discovered — never a hardcoded default)
  fetch  api.open-meteo.com (ECMWF IFS boundary_layer_height), egress-gated
  write  cell.boundary_layer — pblh_m per H3 cell for the latest available UTC hour

PBLH is a COARSE meteorological field: we sample a small GRID of points across the AOI in ONE egress call and
assign every H3 cell its NEAREST sample, carrying native_resolution_km — never upsampling (ADR-0073 §2).

REAL ECMWF only — fetch or fail loudly. NO synthetic fabrication, NO tenant/AOI default. RESUMABLE.

  Env: TENANT · AOI_ID (opt; else discover) · GRID (points/side, default 5) · HOUR (ISO UTC hour; default latest)
       FORECAST_URL (override) · RESUME (1)
"""
import json, math, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_HINT = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""   # NO default — never guess the tenant
GRID = int(os.environ.get("GRID", "5"))
HOUR = os.environ.get("HOUR", "").strip()
SERIES_HOURS_BACK = int(os.environ.get("SERIES_HOURS_BACK", "48"))   # M3: coarse hourly PBLH series at sample points (0 disables)
FORECAST_URL = os.environ.get("FORECAST_URL", "https://api.open-meteo.com/v1/forecast")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/ecmwf-ifs"
NATIVE_KM = 25.0                                   # ECMWF IFS surface field nominal (~0.25°)


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


def _pick_hour_index(times):
    if not times:
        raise RuntimeError("forecast response carried no 'time' axis")
    if HOUR:
        target = HOUR[:13]
        for i, t in enumerate(times):
            if str(t)[:13] == target:
                return i, times[i]
        raise RuntimeError(f"requested HOUR {HOUR} not in returned window {times[0]}..{times[-1]}")
    now = time.strftime("%Y-%m-%dT%H", time.gmtime())
    idx = 0
    for i, t in enumerate(times):
        if str(t)[:13] <= now:
            idx = i
        else:
            break
    return idx, times[idx]


def fetch_pblh(pts):
    """One egress call: ECMWF boundary_layer_height at each sample point; pick one target hour.
    Returns (observed_at_iso, [(lat, lon, pblh_m)...]). REAL or raise."""
    lats = ",".join(f"{la:.4f}" for la, lo in pts)
    lons = ",".join(f"{lo:.4f}" for la, lo in pts)
    url = (f"{FORECAST_URL}?latitude={lats}&longitude={lons}"
           f"&hourly=boundary_layer_height&timezone=GMT&forecast_days=1&past_days=1")
    payload = unwrap(call("/v1/fetch", {"url": url}))
    locs = payload if isinstance(payload, list) else [payload]
    if len(locs) != len(pts) and not (len(locs) == 1 and len(pts) == 1):
        raise RuntimeError(f"forecast returned {len(locs)} locations for {len(pts)} points: {str(payload)[:200]}")
    observed = None
    out = []
    for (la, lo), loc in zip(pts, locs):
        hourly_d = (loc or {}).get("hourly") or {}
        times = hourly_d.get("time") or []
        idx, obs = _pick_hour_index(times)
        observed = observed or obs
        arr = hourly_d.get("boundary_layer_height") or []
        v = arr[idx] if idx < len(arr) else None
        if not isinstance(v, (int, float)):
            raise RuntimeError(f"ECMWF returned no PBLH at point ({la:.3f},{lo:.3f}) hour {obs}: {str(loc)[:160]}")
        out.append((la, lo, round(float(v), 1)))
    obs_iso = str(observed).replace(" ", "T")
    if len(obs_iso) == 13:
        obs_iso += ":00:00"
    if not obs_iso.endswith("Z"):
        obs_iso += "Z"
    return obs_iso, out


def write_point_series(pts, res):
    """M3 temporal depth: write a COARSE hourly SERIES of ECMWF PBLH at the sample points (one cell.boundary_layer
    fact per (sample-point-cell, hour), hour-keyed id under …/cell/boundary_layer/series/…). PBLH is a coarse
    meteorological field, so lags are regional — stored at the ~few dozen sample points, not all 24k cells.
    pm25-features reads these `/series/` facts as the lag anchors. Observed (past+current) hours only."""
    import h3
    past_days = max(1, (SERIES_HOURS_BACK + 23) // 24)
    lats = ",".join(f"{la:.4f}" for la, lo in pts)
    lons = ",".join(f"{lo:.4f}" for la, lo in pts)
    url = (f"{FORECAST_URL}?latitude={lats}&longitude={lons}"
           f"&hourly=boundary_layer_height&timezone=GMT&forecast_days=1&past_days={past_days}")
    payload = unwrap(call("/v1/fetch", {"url": url}))
    locs = payload if isinstance(payload, list) else [payload]
    now = time.strftime("%Y-%m-%dT%H", time.gmtime())
    seen_cells, written = set(), 0
    for (la, lo), loc in zip(pts, locs):
        hc = h3.latlng_to_cell(la, lo, res)
        if hc in seen_cells:
            continue
        seen_cells.add(hc)
        hourly_d = (loc or {}).get("hourly") or {}
        times = hourly_d.get("time") or []
        arr = hourly_d.get("boundary_layer_height") or []
        cen = [round(lo, 6), round(la, 6)]
        for i, t in enumerate(times):
            ts = str(t)
            if ts[:13] > now:
                continue
            v = arr[i] if i < len(arr) else None
            if not isinstance(v, (int, float)):
                continue
            obs = (ts.replace(" ", "T") + (":00" if len(ts) == 16 else ""))
            obs = obs if obs.endswith("Z") else obs + "Z"
            hourkey = ts[:13].replace("-", "").replace("T", "").replace(":", "")
            oid = f"ds:{TENANT}:cell/boundary_layer/series/{hc}/{hourkey}"
            fact = {"schema": "cell.boundary_layer", "id": oid,
                    "cell_ref": {"h3_id": hc, "resolution": res, "centroid": cen},
                    "pblh_m": round(float(v), 1), "native_resolution_km": NATIVE_KM,
                    "method": "ecmwf-ifs-openmeteo-series", "source_ref": SRC, "layer": "L2", "confidence": 0.75,
                    "derived_from": ["ecmwf:ifs"],
                    "prov": {"method": "ecmwf-ifs-openmeteo-series", "source_ref": SRC, "confidence": 0.75,
                             "derived_from": ["ecmwf:ifs"], "native_resolution_km": NATIVE_KM, "observed_at": obs},
                    "observed_at": obs}
            call("/v1/write", {"scope": "cell.boundary_layer", "id": oid, "object": fact,
                               "valid_time": {"from": obs}, "asserted_at": NOW})
            written += 1
    print(f"  wrote {written} cell.boundary_layer SERIES facts at {len(seen_cells)} sample cells (last ~{SERIES_HOURS_BACK}h)", flush=True)
    return written


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
    print(f"aoi {aoi_ref} bbox={bbox} · sampling {GRID}x{GRID}={len(pts)} ECMWF points", flush=True)
    observed_at, samples = fetch_pblh(pts)     # REAL or raise
    vals = [s[2] for s in samples]
    print(f"  ECMWF PBLH across the AOI @ {observed_at}: {min(vals):.0f}–{max(vals):.0f} m ({len(samples)} pts)", flush=True)

    cells = h3_cells(bbox, res)
    if os.environ.get("RESUME", "1") != "0":
        done = set()
        try:
            for r in (call("/v1/query", {"scope": "cell.boundary_layer"}).get("results") or []):
                ob = r.get("object") or r
                if ob.get("observed_at") == observed_at:
                    hid = (ob.get("cell_ref") or {}).get("h3_id")
                    if hid:
                        done.add(hid)
        except Exception:
            pass
        if done:
            before = len(cells); cells = [c for c in cells if c not in done]
            print(f"  resuming: {len(done)} done for {observed_at} → {len(cells)}/{before} remaining", flush=True)
    if not cells:
        print("  nothing to do — all cells already have cell.boundary_layer for this hour"); return 0

    written = 0
    for hc in cells:
        la, lo = h3.cell_to_latlng(hc)
        best = min(samples, key=lambda s: haversine(la, lo, s[0], s[1]))
        fact = {
            "schema": "cell.boundary_layer", "id": f"ds:{TENANT}:cell/boundary_layer/{hc}",
            "cell_ref": {"h3_id": hc, "resolution": res, "centroid": [round(lo, 6), round(la, 6)],
                         "boundary": [[round(x, 6), round(y, 6)] for (x, y) in cell_boundary_lonlat(hc)]},
            "pblh_m": best[2], "native_resolution_km": NATIVE_KM,
            "method": "ecmwf-ifs-openmeteo-nearest", "source_ref": SRC, "layer": "L2", "confidence": 0.75,
            "derived_from": ["ecmwf:ifs"],
            "prov": {"method": "ecmwf-ifs-openmeteo", "source_ref": SRC, "confidence": 0.75,
                     "derived_from": ["ecmwf:ifs"], "native_resolution_km": NATIVE_KM,
                     "grid_points": len(samples), "source_point": [round(best[1], 4), round(best[0], 4)],
                     "observed_at": observed_at},
            "observed_at": observed_at}
        call("/v1/write", {"scope": "cell.boundary_layer", "id": fact["id"], "object": fact,
                           "valid_time": {"from": observed_at}, "asserted_at": NOW})
        written += 1
        if written % 4000 == 0:
            print(f"  …wrote {written}/{len(cells)} cells", flush=True)

    if not written:
        raise SystemExit("FATAL: no cells written (no synthetic fill).")
    print(f"wrote {written} cell.boundary_layer cells from ECMWF (Open-Meteo), hour {observed_at}")

    series = 0
    if SERIES_HOURS_BACK > 0:
        series = write_point_series(pts, res)     # M3: coarse hourly PBLH series at sample points → lag features

    print(f"RESULT: {json.dumps({'source': 'ecmwf-ifs-openmeteo', 'observed_at': observed_at, 'cells': written, 'series_facts': series})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
