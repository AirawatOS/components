#!/usr/bin/env python3
"""corpus-builder — PM2.5 training-corpus builder (ADR-0073 M3.6; certified driver, P0.2).

The BULK-ARCHIVAL ingest lane, now a governed component (was a host-side script). All governed I/O rides THIS
driver's injected RUN_TOKEN: registry reads (the static per-cell layers + the CPCB station coords + AOI), the
brokered Open-Meteo reanalysis egress, and the air.feature_matrix manifest write. Only the two things a small
broker proxy can't do — the ~0.5-0.7 GB/year Vonter national parquet download and the ~20 MB training-Parquet
filestore write — are direct, which is what the UNSANDBOXED ingestion tier with DECLARED egress is for.

  labels  : Vonter india-cpcb-aqi per-year parquet → reference-grade fleet (station-name join to our CPCB
            coords) → 15-min → hourly mean PM2.5 + station weather (AT/RH/WS/WD/RF; wind as u/v vector).
  features: Open-Meteo ARCHIVE (CAMS + ERA5 reanalysis) per station cell over the label window (brokered).
  static  : per-cell baseline layers (buildings/roads/population/…) read once from the registry.
  out     : (station × hour) Parquet → filestore; governed air.feature_matrix manifest → registry.

  Env: TENANT · YEARS ("2023,2024,2025") · FEATURE_SET (manifest tag; default pm25-train-cpcb) ·
       MAX_STATIONS (cap cells for a cheap run; 0 = all) · DATE_FROM / DATE_TO (clamp the feature window) ·
       RUN_TOKEN · AIRAWAT_API · FILESTORE_URL
"""
import os, io, json, time, math, shutil, urllib.request as u, urllib.error, datetime as dt
import pyarrow.parquet as pq, pyarrow as pa
import h3

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
FS = os.environ.get("FILESTORE_URL", "http://filestore:8095")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or "new-delhi"
YEARS = [y.strip() for y in os.environ.get("YEARS", "2023,2024,2025").split(",") if y.strip()]
FEATURE_SET = os.environ.get("FEATURE_SET", "pm25-train-cpcb").strip()
MAX_STATIONS = int(os.environ.get("MAX_STATIONS", "0"))
DATE_FROM = os.environ.get("DATE_FROM", "").strip()
DATE_TO = os.environ.get("DATE_TO", "").strip()
UA = "AirawatOS/corpus-builder (+https://airawat.org; manishsv@gmail.com)"
PM_ARCHIVE = "https://github.com/Vonter/india-cpcb-aqi/releases/download/{y}/cpcb-air-quality-{y}.parquet"
AQ = "https://air-quality-api.open-meteo.com/v1/air-quality"; ERA5 = "https://archive-api.open-meteo.com/v1/archive"
AQ_VARS = [("pm2_5", "pm25_cams"), ("pm10", "pm10_cams"), ("nitrogen_dioxide", "no2_cams"), ("ozone", "o3_cams"), ("sulphur_dioxide", "so2_cams"), ("aerosol_optical_depth", "aod")]
STATIC_SOURCES = {  # scope -> [(fact_field, matrix_col)]
  "cell.buildings": [("built_area_m2", "built_area_m2"), ("building_coverage", "building_coverage"), ("mean_height_m", "building_mean_height_m"), ("built_volume_m3", "built_volume_m3")],
  "cell.builtup": [("value", "builtup_frac")], "cell.greencover": [("value", "greencover_frac")],
  "econ.nightlight": [("avg_radiance", "nightlight_radiance")],
  "cell.roads": [("road_length_m", "road_length_m"), ("road_density_km_per_km2", "road_density")],
  "demographics.population": [("population", "population"), ("people", "population"), ("value", "population")],
  "geo.terrain": [("elevation_m", "elevation_m"), ("slope_deg", "slope_deg")],
  "cell.hydrology": [("flood_susceptibility", "flood_susceptibility"), ("hand_mean_m", "hand_mean_m")],
}
FEATURE_COLS = ["pm25_cams", "pm10_cams", "no2_cams", "o3_cams", "so2_cams", "pblh_m", "aod",
  "built_area_m2", "building_coverage", "building_mean_height_m", "built_volume_m3", "builtup_frac", "greencover_frac",
  "nightlight_radiance", "road_length_m", "road_density", "population", "elevation_m", "slope_deg", "flood_susceptibility", "hand_mean_m",
  "temp_c", "rh_pct", "rain_mm", "wind_ms", "wind_u", "wind_v"]
MET_FIELDS = ["pm", "at", "rh", "rf", "ws", "u", "v"]
LAGS_PM = [1, 3, 6, 24]; LAGS_PBL = [3, 24]
LAG_COLS = [f"pm25_cams_lag{k}" for k in LAGS_PM] + [f"pblh_m_lag{k}" for k in LAGS_PBL]
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def jpost(url, body, hdr=None, timeout=120):
    r = u.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **(hdr or {})}, method="POST")
    return json.loads(u.urlopen(r, timeout=timeout).read())


def rquery(scope):
    out = []; cur = None; pages = 0
    while True:
        b = {"scope": scope, "limit": 500}
        if cur: b["cursor"] = cur
        d = jpost(f"{API}/v1/query", b, {"X-Run-Token": TOKEN})
        res = d.get("results") or []; out += [(x.get("object") or x) for x in res]
        pages += 1; cur = d.get("cursor") or d.get("next_cursor")
        if not cur or not res or pages > 120: break
    return out


def h3of(o):
    sp = o.get("cell_ref") or o.get("space") or {}
    return sp.get("h3_id") or sp.get("h3")


def broker_fetch(url, tries=6):
    for a in range(tries):
        try:
            r = jpost(f"{API}/v1/fetch", {"url": url}, {"X-Run-Token": TOKEN})
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(min(2 ** a, 15)); continue
            return None
        except Exception:
            time.sleep(min(2 ** a, 15)); continue
        st = r.get("upstream_status")
        if st == 429: time.sleep(min(2 ** a, 15)); continue
        if st == 200:
            try: return json.loads(r.get("body") or "{}")
            except Exception: return None
        return None
    return None


def download(url, path, tries=4):
    """Stream a large file to DISK (never into RAM) with retry — declared-egress direct download."""
    for a in range(tries):
        try:
            with u.urlopen(u.Request(url, headers={"User-Agent": UA}), timeout=900) as resp, open(path, "wb") as f:
                shutil.copyfileobj(resp, f, length=1024 * 1024)
            return os.path.getsize(path)
        except Exception as e:
            print(f"  download attempt {a + 1} failed ({type(e).__name__}); retrying…", flush=True); time.sleep(min(2 ** a, 20))
    raise RuntimeError(f"download failed after {tries} tries: {url}")


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set.")
    # AOI res
    aoi = [o for o in rquery("space.aoi") if str(o.get("id", "")).startswith(f"ds:{TENANT}:") and len(o.get("bbox") or []) == 4]
    if not aoi:
        raise SystemExit("FATAL: no space.aoi for this tenant.")
    aoi = max(aoi, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))
    res = int(aoi["h3_res"]); aoi_ref = aoi["id"]
    # station map from our CPCB rows: normalized name -> (lat,lon,h3)
    smap = {}
    for o in rquery("air.sensor_reading"):
        if o.get("source") != "cpcb": continue
        nm = ((o.get("prov") or {}).get("station") or "").strip().lower()
        sp = o.get("space") or o.get("cell_ref") or {}; cen = sp.get("centroid") or []
        if nm and len(cen) == 2 and nm not in smap:
            lat, lon = cen[1], cen[0]
            smap[nm] = {"lat": lat, "lon": lon, "h3": h3.latlng_to_cell(lat, lon, res), "name": (o.get("prov") or {}).get("station")}
    if MAX_STATIONS and len(smap) > MAX_STATIONS:                 # cheap smoke run: cap the fleet
        smap = {k: smap[k] for k in sorted(smap)[:MAX_STATIONS]}
    print(f"station map: {len(smap)} CPCB stations · res={res}", flush=True)
    cells = {s["h3"]: (s["lat"], s["lon"]) for s in smap.values()}
    # static features per cell
    static = {}
    for scope, fields in STATIC_SOURCES.items():
        for o in rquery(scope):
            h = h3of(o)
            if h in cells:
                d = static.setdefault(h, {})
                for ff, col in fields:
                    if o.get(ff) is not None and d.get(col) is None: d[col] = o.get(ff)
    print(f"static features loaded for {len(static)}/{len(cells)} cells", flush=True)

    labels = {}; wanted = set(smap.keys())

    def _f(v):
        try:
            f = float(v); return f if f == f else None
        except (TypeError, ValueError): return None
    VCOLS = ["Station Name", "Timestamp", "PM2.5 (µg/m³)", "AT (°C)", "RH (%)", "WS (m/s)", "WD (deg)", "RF (mm)"]
    for y in YEARS:
        url = PM_ARCHIVE.format(y=y); path = f"/tmp/vonter_{y}.parquet"
        print(f"[{y}] downloading {url} → {path} …", flush=True)
        sz = download(url, path)
        print(f"[{y}] {sz} bytes on disk; reading parquet columns…", flush=True)
        t = pq.read_table(path, columns=VCOLS)
        cols = {c: t.column(c).to_pylist() for c in VCOLS}
        del t
        try: os.remove(path)
        except OSError: pass
        nm_a, ts_a = cols["Station Name"], cols["Timestamp"]
        pm_a, at_a, rh_a, ws_a, wd_a, rf_a = cols["PM2.5 (µg/m³)"], cols["AT (°C)"], cols["RH (%)"], cols["WS (m/s)"], cols["WD (deg)"], cols["RF (mm)"]
        n = 0
        for i in range(len(nm_a)):
            nm = nm_a[i]; pm = _f(pm_a[i])
            if nm is None or pm is None: continue
            key_nm = str(nm).strip().lower()
            if key_nm not in wanted: continue
            hh = ts_a[i].strftime("%Y-%m-%dT%H:00:00Z")
            k = (key_nm, hh); slot = labels.get(k)
            if slot is None: slot = labels[k] = {f: [0.0, 0] for f in MET_FIELDS}

            def add(f, v):
                if v is not None: slot[f][0] += v; slot[f][1] += 1
            add("pm", pm); add("at", _f(at_a[i])); add("rh", _f(rh_a[i])); add("rf", _f(rf_a[i]))
            ws = _f(ws_a[i]); wd = _f(wd_a[i]); add("ws", ws)
            if ws is not None and wd is not None:
                r = math.radians(wd); add("u", -ws * math.sin(r)); add("v", -ws * math.cos(r))
            n += 1
        print(f"[{y}] matched {n} 15-min rows → {len(labels)} station-hours so far", flush=True)

    if not labels:
        print("FAIL: no labels matched — check station-name join."); return 1
    hours = sorted({hh for (_, hh) in labels.keys()})
    d0, d1 = hours[0][:10], hours[-1][:10]
    if DATE_FROM: d0 = max(d0, DATE_FROM)
    if DATE_TO: d1 = min(d1, DATE_TO)
    print(f"label window {d0}..{d1} · {len(labels)} station-hours · {len(cells)} cells", flush=True)

    # FEATURES: per cell, brokered Open-Meteo archive over [d0,d1]
    cams = {}; pbl = {}

    def chunks(a, b):
        A = dt.datetime.strptime(a, "%Y-%m-%d"); B = dt.datetime.strptime(b, "%Y-%m-%d")
        while A <= B:
            C = min(A + dt.timedelta(days=180), B); yield A.strftime("%Y-%m-%d"), C.strftime("%Y-%m-%d"); A = C + dt.timedelta(days=1)
    for i, (h, (lat, lon)) in enumerate(cells.items()):
        for cf, ct in chunks(d0, d1):
            aq = broker_fetch(f"{AQ}?latitude={lat:.4f}&longitude={lon:.4f}&start_date={cf}&end_date={ct}&hourly={','.join(v[0] for v in AQ_VARS)}&domains=cams_global&timezone=UTC")
            hh = (aq or {}).get("hourly") or {}
            for j, tt in enumerate(hh.get("time") or []):
                rec = {}
                for src, dst in AQ_VARS:
                    arr = hh.get(src) or []; val = arr[j] if j < len(arr) else None
                    if isinstance(val, (int, float)): rec[dst] = round(val, 3)
                if rec: cams[(h, str(tt)[:13])] = rec
            ar = broker_fetch(f"{ERA5}?latitude={lat:.4f}&longitude={lon:.4f}&start_date={cf}&end_date={ct}&hourly=boundary_layer_height&timezone=UTC")
            h2 = (ar or {}).get("hourly") or {}; pv = h2.get("boundary_layer_height") or []
            for j, tt in enumerate(h2.get("time") or []):
                if j < len(pv) and isinstance(pv[j], (int, float)): pbl[(h, str(tt)[:13])] = round(pv[j], 1)
        print(f"  features cell {i + 1}/{len(cells)} {h[:10]}… cams={sum(1 for k in cams if k[0] == h)} pbl={sum(1 for k in pbl if k[0] == h)}", flush=True)

    # ASSEMBLE rows
    name2cell = {nm: smap[nm]["h3"] for nm in smap}

    def mean(agg, f, nd=2):
        s, c = agg[f]; return round(s / c, nd) if c else None
    rows = []
    for (nm, hh), agg in labels.items():
        if not agg["pm"][1]: continue
        if DATE_FROM and hh[:10] < d0: continue
        if DATE_TO and hh[:10] > d1: continue
        h = name2cell[nm]; lat, lon = cells[h]
        row = {"h3_id": h, "resolution": res, "lon": round(lon, 5), "lat": round(lat, 5), "observed_at": hh, "station": smap[nm]["name"],
               "pm25_obs": mean(agg, "pm")}
        for col in FEATURE_COLS + LAG_COLS: row[col] = None
        row["temp_c"] = mean(agg, "at"); row["rh_pct"] = mean(agg, "rh"); row["rain_mm"] = mean(agg, "rf", 3)
        row["wind_ms"] = mean(agg, "ws"); row["wind_u"] = mean(agg, "u"); row["wind_v"] = mean(agg, "v")
        hk = hh[:13]
        for col, val in (cams.get((h, hk)) or {}).items(): row[col] = val
        if (h, hk) in pbl: row["pblh_m"] = pbl[(h, hk)]
        for col, val in (static.get(h) or {}).items(): row[col] = val
        try:
            t0 = dt.datetime.strptime(hk, "%Y-%m-%dT%H")
            for k in LAGS_PM:
                kk = (t0 - dt.timedelta(hours=k)).strftime("%Y-%m-%dT%H"); row[f"pm25_cams_lag{k}"] = (cams.get((h, kk)) or {}).get("pm25_cams")
            for k in LAGS_PBL:
                kk = (t0 - dt.timedelta(hours=k)).strftime("%Y-%m-%dT%H"); row[f"pblh_m_lag{k}"] = pbl.get((h, kk))
        except Exception: pass
        rows.append(row)
    if not rows:
        print("FAIL: no rows assembled after window clamp."); return 1
    aligned = sum(1 for r in rows if r.get("pm25_cams") is not None)
    print(f"assembled {len(rows)} rows · {aligned} with aligned CAMS features · {len(set(r['h3_id'] for r in rows))} stations", flush=True)

    # PARQUET → filestore (direct; a ~20 MB analytical blob, over the small broker proxy cap)
    cols = ["h3_id", "resolution", "lon", "lat", "observed_at", "station"] + FEATURE_COLS + LAG_COLS + ["pm25_obs"]
    table = pa.table({c: [r.get(c) for r in rows] for c in cols})
    buf = io.BytesIO(); pq.write_table(table, buf); blob = buf.getvalue()
    fr = u.Request(f"{FS}/files", data=blob, headers={"Content-Type": "application/octet-stream"}, method="POST")
    fid = json.loads(u.urlopen(fr, timeout=300).read())["id"]
    print(f"parquet {len(blob)} bytes → filestore {fid}", flush=True)

    # MANIFEST → registry (governed, via THIS driver's run-token)
    win = f"{d0}_{d1}".replace("-", "")
    fm_id = f"ds:{TENANT}:air/feature_matrix/{FEATURE_SET}-{win}"
    fm = {"schema": "air.feature_matrix", "id": fm_id, "feature_set": FEATURE_SET, "aoi": aoi_ref, "file_id": fid,
          "format": "parquet", "rows": len(rows), "n_features": len(FEATURE_COLS) + len(LAG_COLS),
          "columns": cols, "feature_cols": FEATURE_COLS + LAG_COLS, "label_col": "pm25_obs", "labels_present": len(rows),
          "size_bytes": len(blob), "method": "cpcb-corpus-builder", "observed_at": hours[-1], "built_at": NOW,
          "prov": {"component": "ds:airawatdeveloper:driver/corpus-builder", "method": "cpcb-corpus-builder",
                   "sources": ["cpcb-caaqms/dpcc via Vonter india-cpcb-aqi (ODbL/CPCB)", "cams:reanalysis:open-meteo", "era5:reanalysis:open-meteo"],
                   "stations": len(set(r["h3_id"] for r in rows)), "window": [d0, d1], "years": YEARS, "built_at": NOW}}
    jpost(f"{API}/v1/write", {"scope": "air.feature_matrix", "id": fm_id, "object": fm, "valid_time": {"from": hours[-1]}, "asserted_at": NOW}, {"X-Run-Token": TOKEN})
    print(f"wrote manifest {fm_id}", flush=True)
    print("RESULT: " + json.dumps({"rows": len(rows), "aligned": aligned, "stations": len(set(r['h3_id'] for r in rows)), "file_id": fid, "feature_set": FEATURE_SET, "window": [d0, d1], "manifest": fm_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
