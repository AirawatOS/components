#!/usr/bin/env python3
"""feature-backfill driver: HISTORICAL hourly features at the label-station cells (ADR-0073 M3.6).

The openaq-archive driver (M3.5) gave us MONTHS of historical PM2.5 LABELS, but the live feature drivers
(cams-regional / era5-pblh / maiac-aod) write only the LATEST hour. Supervised training needs each label's
FEATURES time-aligned to the SAME hour. This driver closes that gap for the cells that actually carry labels:
for every station cell (a cell with ≥1 air.sensor_reading) it fetches the CAMS reanalysis + ERA5 reanalysis
for the whole label window from Open-Meteo's KEYLESS ARCHIVE APIs and writes hourly SERIES facts:

  • cell.cams            series  (pm25/pm10/no2/o3/so2)   ← air-quality-api /air-quality?start_date&end_date  (CAMS)
  • cell.aod             series  (aod)                    ← same call, aerosol_optical_depth
  • cell.boundary_layer  series  (pblh_m)                 ← archive-api /archive  (ERA5 reanalysis)

It reuses the EXACT `…/cell/<subject>/series/{h3}/{YYYYMMDDHH}` id pattern + fact shape the cams/pblh drivers
already write, so (a) STRICT schema validation passes and (b) pm25-features' existing lag reader + its new
training mode pick them up unchanged. The window is derived from the labels themselves (min..max observed_at)
so features and labels always line up; DATE_FROM/DATE_TO/DAYS_BACK override. Real reanalysis only — a source
that errors is skipped for that cell, never fabricated; zero series written fails loudly (ADR: no synthetic data).

  Env: TENANT/broker-context · AOI_ID (opt) · DATE_FROM/DATE_TO (YYYY-MM-DD) or DAYS_BACK · MAX_CELLS · CHUNK_DAYS
"""
import json, os, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
import h3

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "")
AQ = "https://air-quality-api.open-meteo.com/v1/air-quality"      # CAMS reanalysis (start_date/end_date)
ERA5 = "https://archive-api.open-meteo.com/v1/archive"            # ERA5 reanalysis
NATIVE_KM = 40.0                                                  # CAMS global ≈40 km; PBLH ≈25 km (coarse — carried)
SRC_CAMS = "ds:airawat:ref/source/cams"
SRC_ERA5 = "ds:airawat:ref/source/ecmwf-ifs"
COMPONENT = "ds:airawat:driver/feature-backfill"
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
MAX_CELLS = int(os.environ.get("MAX_CELLS", "50"))
CHUNK_DAYS = int(os.environ.get("CHUNK_DAYS", "60"))
# Only backfill cells with ≥ MIN_LABELS label-hours (a single-snapshot CPCB station isn't worth 121 days of
# features — the training value is in cells with a real hourly history, e.g. the OpenAQ archive station). And
# backfill each cell over ITS OWN label span (± LAG_PAD_DAYS for lag features), not the global window — so a
# sparse cell costs a few days, a dense cell the full window. A global DATE_FROM/DATE_TO still overrides.
MIN_LABELS = int(os.environ.get("MIN_LABELS", "24"))
LAG_PAD_DAYS = int(os.environ.get("LAG_PAD_DAYS", "2"))
# Open-Meteo air-quality field → our cell.cams / cell.aod field
AQ_VARS = [("pm2_5", "pm25"), ("pm10", "pm10"), ("nitrogen_dioxide", "no2"),
           ("ozone", "o3"), ("sulphur_dioxide", "so2"), ("aerosol_optical_depth", "aod")]


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=90).read())


def _fetch(url, tries=5):
    """Brokered keyless egress → parsed JSON (or None). Backs off on the broker's 429."""
    for attempt in range(tries):
        try:
            r = _call("/v1/fetch", {"url": url})
        except urllib.error.HTTPError as e:
            if e.code == 429:
                w = min(2 ** attempt, 15); print(f"  broker 429 → {w}s"); time.sleep(w); continue
            return None
        st = r.get("upstream_status")
        if st == 429:
            time.sleep(min(2 ** attempt, 15)); continue
        if st == 200:
            try:
                return json.loads(r.get("body") or "{}")
            except Exception:
                return None
        print(f"  archive upstream {st} for {url[:90]}…")
        return None
    return None


def _tenant():
    try:
        req = urllib.request.Request(API + "/v1/context", headers={"X-Run-Token": TOKEN})
        return (json.loads(urllib.request.urlopen(req, timeout=20).read()) or {}).get("tenant")
    except Exception:
        return None


def _num(v):
    try:
        f = float(v); return f if f == f else None
    except (TypeError, ValueError):
        return None


def _hourkey(ts):
    return str(ts)[:13].replace("-", "").replace("T", "").replace(" ", "").replace(":", "")


def _obs(ts):
    s = str(ts).replace(" ", "T")
    if len(s) == 16:
        s += ":00"
    return s if s.endswith("Z") else s + "Z"


def _label_cells(T, res):
    """Distinct station cells that carry a label → {h3: {lat, lon, min, max, n}} (per-cell label span + count)."""
    cells, cursor, pages = {}, None, 0
    while True:
        body = {"scope": "air.sensor_reading", "limit": 500}
        if cursor:
            body["cursor"] = cursor
        r = _call("/v1/query", body)
        res_rows = r.get("results") or []
        for row in res_rows:
            o = row.get("object") or row
            sp = o.get("cell_ref") or o.get("space") or {}
            hc = sp.get("h3") or sp.get("h3_id")
            cen = sp.get("centroid") or []
            ob = o.get("observed_at")
            if not hc or len(cen) != 2:
                continue
            c = cells.setdefault(hc, {"lat": cen[1], "lon": cen[0], "min": ob, "max": ob, "n": 0})
            c["n"] += 1
            if ob:
                c["min"] = ob if not c["min"] or ob < c["min"] else c["min"]
                c["max"] = ob if not c["max"] or ob > c["max"] else c["max"]
        pages += 1
        cursor = r.get("cursor") or r.get("next_cursor")
        if not cursor or not res_rows or pages > 60:
            break
    return cells


def _window(omin, omax):
    df, dt = os.environ.get("DATE_FROM", "").strip(), os.environ.get("DATE_TO", "").strip()
    if df and dt:
        return df, dt
    if os.environ.get("DAYS_BACK"):
        d1 = datetime.strptime(NOW[:10], "%Y-%m-%d") - timedelta(days=3)
        d0 = d1 - timedelta(days=int(os.environ["DAYS_BACK"]))
        return d0.strftime("%Y-%m-%d"), d1.strftime("%Y-%m-%d")
    # default: exactly the label window
    return (omin or NOW)[:10], (omax or NOW)[:10]


def _chunks(d0, d1):
    a = datetime.strptime(d0, "%Y-%m-%d"); b = datetime.strptime(d1, "%Y-%m-%d")
    while a <= b:
        c = min(a + timedelta(days=CHUNK_DAYS - 1), b)
        yield a.strftime("%Y-%m-%d"), c.strftime("%Y-%m-%d")
        a = c + timedelta(days=1)


def _write(scope, oid, fact, obs):
    _call("/v1/write", {"scope": scope, "id": oid, "object": fact,
                        "valid_time": {"from": obs}, "asserted_at": NOW})


def backfill_cell(T, hc, lat, lon, res, d0, d1):
    cen = [round(lon, 6), round(lat, 6)]
    n_cams = n_aod = n_pbl = 0
    for cf, ct in _chunks(d0, d1):
        # --- CAMS reanalysis (air-quality archive) → cell.cams + cell.aod hourly series
        aq = _fetch(f"{AQ}?latitude={lat:.4f}&longitude={lon:.4f}&start_date={cf}&end_date={ct}"
                    f"&hourly={','.join(v[0] for v in AQ_VARS)}&domains=cams_global&timezone=UTC")
        h = (aq or {}).get("hourly") or {}
        times = h.get("time") or []
        for i, t in enumerate(times):
            obs, hk = _obs(t), _hourkey(t)
            rec = {}
            for src, dst in AQ_VARS:
                arr = h.get(src) or []
                rec[dst] = round(v, 3) if (v := _num(arr[i] if i < len(arr) else None)) is not None else None
            if rec.get("pm25") is not None or rec.get("pm10") is not None:
                oid = f"ds:{T}:cell/cams/series/{hc}/{hk}"
                fact = {"schema": "cell.cams", "id": oid,
                        "cell_ref": {"h3_id": hc, "resolution": res, "centroid": cen},
                        "pm25": rec.get("pm25"), "pm10": rec.get("pm10"), "no2": rec.get("no2"),
                        "o3": rec.get("o3"), "so2": rec.get("so2"), "native_resolution_km": NATIVE_KM,
                        "method": "cams-reanalysis-openmeteo-archive", "source_ref": SRC_CAMS, "layer": "L2",
                        "confidence": 0.7, "derived_from": ["cams:global:reanalysis"],
                        "prov": {"method": "cams-reanalysis-openmeteo-archive", "source_ref": SRC_CAMS,
                                 "confidence": 0.7, "derived_from": ["cams:global:reanalysis"],
                                 "native_resolution_km": NATIVE_KM, "observed_at": obs},
                        "observed_at": obs}
                _write("cell.cams", oid, {k: v for k, v in fact.items() if v is not None}, obs); n_cams += 1
            if rec.get("aod") is not None:
                oid = f"ds:{T}:cell/aod/series/{hc}/{hk}"
                fact = {"schema": "cell.aod", "id": oid,
                        "cell_ref": {"h3_id": hc, "resolution": res, "centroid": cen},
                        "aod": round(rec["aod"], 4), "native_resolution_km": NATIVE_KM,
                        "method": "cams-reanalysis-openmeteo-archive", "source_ref": SRC_CAMS, "layer": "L2",
                        "confidence": 0.7, "derived_from": ["cams:global:reanalysis"],
                        "prov": {"method": "cams-reanalysis-openmeteo-archive", "source_ref": SRC_CAMS,
                                 "confidence": 0.7, "derived_from": ["cams:global:reanalysis"],
                                 "native_resolution_km": NATIVE_KM, "observed_at": obs},
                        "observed_at": obs}
                _write("cell.aod", oid, {k: v for k, v in fact.items() if v is not None}, obs); n_aod += 1
        # --- ERA5 reanalysis (archive) → cell.boundary_layer hourly series
        ar = _fetch(f"{ERA5}?latitude={lat:.4f}&longitude={lon:.4f}&start_date={cf}&end_date={ct}"
                    f"&hourly=boundary_layer_height&timezone=UTC")
        h2 = (ar or {}).get("hourly") or {}
        t2, pbl = h2.get("time") or [], h2.get("boundary_layer_height") or []
        for i, t in enumerate(t2):
            v = _num(pbl[i] if i < len(pbl) else None)
            if v is None:
                continue
            obs, hk = _obs(t), _hourkey(t)
            oid = f"ds:{T}:cell/boundary_layer/series/{hc}/{hk}"
            fact = {"schema": "cell.boundary_layer", "id": oid,
                    "cell_ref": {"h3_id": hc, "resolution": res, "centroid": cen},
                    "pblh_m": round(v, 1), "native_resolution_km": 25.0,
                    "method": "era5-reanalysis-openmeteo-archive", "source_ref": SRC_ERA5, "layer": "L2",
                    "confidence": 0.7, "derived_from": ["era5:reanalysis"],
                    "prov": {"method": "era5-reanalysis-openmeteo-archive", "source_ref": SRC_ERA5,
                             "confidence": 0.7, "derived_from": ["era5:reanalysis"],
                             "native_resolution_km": 25.0, "observed_at": obs},
                    "observed_at": obs}
            _write("cell.boundary_layer", oid, {k: v2 for k, v2 in fact.items() if v2 is not None}, obs); n_pbl += 1
    print(f"  cell {hc[:12]}… ({lat:.3f},{lon:.3f}): cams={n_cams} aod={n_aod} pblh={n_pbl}", flush=True)
    return n_cams, n_aod, n_pbl


def main():
    T = _tenant()
    if not T:
        print("FAIL: could not resolve the run tenant (/v1/context)."); return 1
    if AOI_ID:
        aoi = _call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object")
    else:
        rows = _call("/v1/query", {"scope": "space.aoi", "limit": 500}).get("results", [])
        mine = [(r.get("object") or r) for r in rows
                if str((r.get("object") or r).get("id", "")).startswith(f"ds:{T}:")
                and len((r.get("object") or r).get("bbox") or []) == 4]
        aoi = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1]))) if mine else None
    if not aoi:
        print(f"FAIL: no space.aoi for tenant '{T}'. Nothing written."); return 1
    res = int(aoi["h3_res"])
    cells = _label_cells(T, res)
    if not cells:
        print("FAIL: no label cells (air.sensor_reading) — run air-sensors / openaq-archive first."); return 1
    gdf, gdt = os.environ.get("DATE_FROM", "").strip(), os.environ.get("DATE_TO", "").strip()
    # keep only cells worth backfilling (enough label-hours to train on), densest first
    worth = sorted([(hc, c) for hc, c in cells.items() if c["n"] >= MIN_LABELS], key=lambda x: -x[1]["n"])
    print(f"tenant={T} res={res} · {len(cells)} label cells · {len(worth)} with ≥{MIN_LABELS} labels → backfilling top {min(len(worth),MAX_CELLS)}", flush=True)
    if not worth:
        print(f"FAIL: no cell has ≥{MIN_LABELS} label-hours (lower MIN_LABELS to backfill sparse cells)."); return 1

    tc = ta = tp = 0
    for hc, c in worth[:MAX_CELLS]:
        if gdf and gdt:
            d0, d1 = gdf, gdt                                     # explicit global override
        elif os.environ.get("DAYS_BACK"):
            d0, d1 = _window(None, None)
        else:                                                    # this cell's OWN label span, padded for lags
            span0 = (datetime.strptime(str(c["min"])[:10], "%Y-%m-%d") - timedelta(days=LAG_PAD_DAYS)).strftime("%Y-%m-%d")
            d0, d1 = span0, str(c["max"])[:10]
        print(f"  → cell {hc[:12]}… n_labels={c['n']} window={d0}..{d1}", flush=True)
        cc, a, p = backfill_cell(T, hc, c["lat"], c["lon"], res, d0, d1)
        tc += cc; ta += a; tp += p
    if not (tc or tp):
        print("No REAL reanalysis series written (nothing persisted; no synthetic fill)."); return 1
    print(f"backfilled REAL hourly features: cell.cams={tc} cell.aod={ta} cell.boundary_layer={tp} over {len(cells)} cell(s)")
    print(f"RESULT: {json.dumps({'cells': len(cells), 'cams': tc, 'aod': ta, 'pblh': tp, 'window': [d0, d1]})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
