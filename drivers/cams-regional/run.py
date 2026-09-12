#!/usr/bin/env python3
"""cams-regional — REGIONAL BACKGROUND AIR from CAMS (Open-Meteo air-quality API) (ADR-0073 M2).

CAMS (Copernicus Atmosphere Monitoring Service, ECMWF) is the global atmospheric-composition analysis/forecast.
It is the large-scale *context* term a local PM2.5 model needs: the regional haze a city sits inside, before its
own emissions add on top (brief §23.6). Open-Meteo's air-quality API serves CAMS global (~40 km) keyless, so this
driver is a host subprocess like rainfall-openmeteo / weather-forecast.

Its ONLY I/O is the kernel broker:
  read   the tenant's space.aoi frame (discovered — never a hardcoded default)
  fetch  air-quality-api.open-meteo.com (CAMS global), egress-gated
  write  cell.cams — pm2.5/pm10/no2/o3/so2 (µg/m³) per H3 cell for the latest available UTC hour

CAMS is a COARSE field (~40 km): we sample a small GRID of points across the AOI in ONE egress call, and assign
every H3 cell its NEAREST sample. We DO NOT manufacture spatial detail — each cell carries native_resolution_km
(~40) and its source sample point, so pm25-features treats it honestly as the regional background, not a local
measurement (ADR-0073 §2; AOD/CAMS ≠ local PM2.5).

REAL CAMS only — fetch or fail loudly. NO synthetic fabrication, NO tenant/AOI default. RESUMABLE (skips cells
already written for the target hour).

  Env: TENANT · AOI_ID (opt; else discover) · GRID (points/side, default 5) · HOUR (ISO UTC hour; default latest)
       AQ_URL (override) · RESUME (1)
"""
import json, math, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_HINT = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""   # NO default — never guess the tenant
GRID = int(os.environ.get("GRID", "5"))
HOUR = os.environ.get("HOUR", "").strip()          # pin a specific UTC hour, else latest available
SERIES_HOURS_BACK = int(os.environ.get("SERIES_HOURS_BACK", "48"))   # M3: also write a coarse hourly SERIES at the
                                                                     # sample points (for temporal-lag features); 0 disables
AQ_URL = os.environ.get("AQ_URL", "https://air-quality-api.open-meteo.com/v1/air-quality")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/cams"
NATIVE_KM = 40.0                                   # CAMS global nominal grid (~0.4°)
# Open-Meteo air-quality variable → our field name
VARS = [("pm2_5", "pm25"), ("pm10", "pm10"), ("nitrogen_dioxide", "no2"),
        ("ozone", "o3"), ("sulphur_dioxide", "so2")]


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
    """Choose the row to write. If HOUR is pinned, match it exactly; else the LATEST hour that is not in the
    future (CAMS forecast arrays run past 'now'). times are UTC ISO strings (timezone=GMT)."""
    if not times:
        raise RuntimeError("air-quality response carried no 'time' axis")
    if HOUR:
        target = HOUR[:13]                                   # 'YYYY-MM-DDTHH'
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


def fetch_cams(pts):
    """One egress call: CAMS global values at each sample point for a short hourly window; pick one target hour.
    Returns (observed_at_iso, [(lat, lon, {field: value})...]). REAL or raise."""
    lats = ",".join(f"{la:.4f}" for la, lo in pts)
    lons = ",".join(f"{lo:.4f}" for la, lo in pts)
    hourly = ",".join(v[0] for v in VARS)
    url = (f"{AQ_URL}?latitude={lats}&longitude={lons}"
           f"&hourly={hourly}&domains=cams_global&timezone=GMT&forecast_days=1&past_days=1")
    payload = unwrap(call("/v1/fetch", {"url": url}))
    locs = payload if isinstance(payload, list) else [payload]
    if len(locs) != len(pts) and not (len(locs) == 1 and len(pts) == 1):
        raise RuntimeError(f"air-quality returned {len(locs)} locations for {len(pts)} points: {str(payload)[:200]}")
    observed = None
    out = []
    for (la, lo), loc in zip(pts, locs):
        hourly_d = (loc or {}).get("hourly") or {}
        times = hourly_d.get("time") or []
        idx, obs = _pick_hour_index(times)
        observed = observed or obs
        rec = {}
        for src_name, field in VARS:
            arr = hourly_d.get(src_name) or []
            v = arr[idx] if idx < len(arr) else None
            rec[field] = round(float(v), 2) if isinstance(v, (int, float)) else None
        if rec.get("pm25") is None and rec.get("pm10") is None:
            raise RuntimeError(f"CAMS returned no PM at point ({la:.3f},{lo:.3f}) hour {obs}: {str(loc)[:160]}")
        out.append((la, lo, rec))
    # normalise observed to a Z-suffixed UTC instant
    obs_iso = (str(observed).replace(" ", "T"))
    if len(obs_iso) == 13:                                    # 'YYYY-MM-DDTHH'
        obs_iso += ":00:00"
    if not obs_iso.endswith("Z"):
        obs_iso += "Z"
    return obs_iso, out


def write_point_series(pts, res):
    """M3 temporal depth: write a COARSE hourly SERIES of CAMS values at the sample points (one cell.cams fact per
    (sample-point-cell, hour), hour-keyed id under …/cell/cams/series/…). CAMS is a ~40 km field, so its lags are
    regional — storing them at the ~few dozen sample points (not all 24k cells) is honest + cheap. pm25-features
    reads these `/series/` facts as the lag anchors. Only past+current hours are written (an observed series)."""
    import h3
    past_days = max(1, (SERIES_HOURS_BACK + 23) // 24)
    lats = ",".join(f"{la:.4f}" for la, lo in pts)
    lons = ",".join(f"{lo:.4f}" for la, lo in pts)
    hourly = ",".join(v[0] for v in VARS)
    url = (f"{AQ_URL}?latitude={lats}&longitude={lons}"
           f"&hourly={hourly}&domains=cams_global&timezone=GMT&forecast_days=1&past_days={past_days}")
    payload = unwrap(call("/v1/fetch", {"url": url}))
    locs = payload if isinstance(payload, list) else [payload]
    now = time.strftime("%Y-%m-%dT%H", time.gmtime())
    seen_cells, written = set(), 0
    for (la, lo), loc in zip(pts, locs):
        hc = h3.latlng_to_cell(la, lo, res)
        if hc in seen_cells:                                # two sample points in one cell → one series
            continue
        seen_cells.add(hc)
        hourly_d = (loc or {}).get("hourly") or {}
        times = hourly_d.get("time") or []
        cen = [round(lo, 6), round(la, 6)]
        for i, t in enumerate(times):
            ts = str(t)
            if ts[:13] > now:                               # observed series only — skip future forecast hours
                continue
            obs = (ts.replace(" ", "T") + (":00" if len(ts) == 16 else ""))
            obs = obs if obs.endswith("Z") else obs + "Z"
            rec = {}
            for src_name, field in VARS:
                arr = hourly_d.get(src_name) or []
                v = arr[i] if i < len(arr) else None
                rec[field] = round(float(v), 2) if isinstance(v, (int, float)) else None
            if rec.get("pm25") is None and rec.get("pm10") is None:
                continue
            hourkey = ts[:13].replace("-", "").replace("T", "").replace(":", "")   # YYYYMMDDHH
            oid = f"ds:{TENANT}:cell/cams/series/{hc}/{hourkey}"
            fact = {"schema": "cell.cams", "id": oid,
                    "cell_ref": {"h3_id": hc, "resolution": res, "centroid": cen},
                    "pm25": rec.get("pm25"), "pm10": rec.get("pm10"), "no2": rec.get("no2"),
                    "o3": rec.get("o3"), "so2": rec.get("so2"), "native_resolution_km": NATIVE_KM,
                    "method": "cams-global-openmeteo-series", "source_ref": SRC, "layer": "L2", "confidence": 0.7,
                    "derived_from": ["cams:global"],
                    "prov": {"method": "cams-global-openmeteo-series", "source_ref": SRC, "confidence": 0.7,
                             "derived_from": ["cams:global"], "native_resolution_km": NATIVE_KM, "observed_at": obs},
                    "observed_at": obs}
            fact = {k: v for k, v in fact.items() if v is not None}
            call("/v1/write", {"scope": "cell.cams", "id": oid, "object": fact,
                               "valid_time": {"from": obs}, "asserted_at": NOW})
            written += 1
    print(f"  wrote {written} cell.cams SERIES facts at {len(seen_cells)} sample cells (last ~{SERIES_HOURS_BACK}h, for lag features)", flush=True)
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
    print(f"aoi {aoi_ref} bbox={bbox} · sampling {GRID}x{GRID}={len(pts)} CAMS points", flush=True)
    observed_at, samples = fetch_cams(pts)     # REAL or raise
    pm = [s[2]["pm25"] for s in samples if isinstance(s[2].get("pm25"), (int, float))]
    if pm:
        print(f"  CAMS PM2.5 across the AOI @ {observed_at}: {min(pm):.0f}–{max(pm):.0f} µg/m³ ({len(samples)} pts)", flush=True)

    cells = h3_cells(bbox, res)
    if os.environ.get("RESUME", "1") != "0":
        done = set()
        try:
            for r in (call("/v1/query", {"scope": "cell.cams"}).get("results") or []):
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
        print("  nothing to do — all cells already have cell.cams for this hour"); return 0

    written = 0
    for hc in cells:
        la, lo = h3.cell_to_latlng(hc)
        best = min(samples, key=lambda s: haversine(la, lo, s[0], s[1]))
        rec = best[2]
        fact = {
            "schema": "cell.cams", "id": f"ds:{TENANT}:cell/cams/{hc}",
            "cell_ref": {"h3_id": hc, "resolution": res, "centroid": [round(lo, 6), round(la, 6)],
                         "boundary": [[round(x, 6), round(y, 6)] for (x, y) in cell_boundary_lonlat(hc)]},
            "pm25": rec.get("pm25"), "pm10": rec.get("pm10"), "no2": rec.get("no2"),
            "o3": rec.get("o3"), "so2": rec.get("so2"),
            "native_resolution_km": NATIVE_KM,
            "method": "cams-global-openmeteo-nearest", "source_ref": SRC, "layer": "L2", "confidence": 0.7,
            "derived_from": ["cams:global"],
            "prov": {"method": "cams-global-openmeteo", "source_ref": SRC, "confidence": 0.7,
                     "derived_from": ["cams:global"], "native_resolution_km": NATIVE_KM,
                     "grid_points": len(samples), "source_point": [round(best[1], 4), round(best[0], 4)],
                     "observed_at": observed_at},
            "observed_at": observed_at}
        fact = {k: v for k, v in fact.items() if v is not None}   # omit unset optionals (house rule R4)
        call("/v1/write", {"scope": "cell.cams", "id": fact["id"], "object": fact,
                           "valid_time": {"from": observed_at}, "asserted_at": NOW})
        written += 1
        if written % 4000 == 0:
            print(f"  …wrote {written}/{len(cells)} cells", flush=True)

    if not written:
        raise SystemExit("FATAL: no cells written (no synthetic fill).")
    print(f"wrote {written} cell.cams cells from CAMS global (Open-Meteo), hour {observed_at}")

    series = 0
    if SERIES_HOURS_BACK > 0:
        series = write_point_series(pts, res)     # M3: coarse hourly series at sample points → lag features

    print(f"RESULT: {json.dumps({'source': 'cams-global-openmeteo', 'observed_at': observed_at, 'cells': written, 'series_facts': series})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
