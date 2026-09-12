#!/usr/bin/env python3
"""pm25-features — the PM2.5 FEATURE STORE builder (ADR-0073 M1 scaffold → M3 feature store).

Assembles the `(h3 × hour)` feature matrix that a PM2.5 nowcast/forecast model trains and infers on. It reads the
governed per-cell facts already in the registry and JOINS them on `h3_id` into one row per cell for a target hour,
then MATERIALISES the matrix to the filestore as a content-addressed blob — the ADR-0073 §1 seam ("governed facts
in, an efficient columnar training set out"). The registry is the staged/h3 governed layer; the filestore blob is
`features/`.

Its ONLY I/O is the kernel broker:
  read   space.aoi (the frame) + the per-cell feature facts (cell.cams, cell.boundary_layer, cell.rainfall,
         cell.population, geo.terrain, cell.hydrology) + air.sensor_reading (ground-truth label, when present)
  put    the assembled matrix → filestore (/v1/file/put), content-addressed → a stable feature-set id

M1 SCOPE (this scaffold):
  • one target hour (the latest cell.cams hour, or HOUR=<iso>), the cell set defined by cell.cams
  • the columns available today: regional background (CAMS), boundary-layer height (ECMWF), rainfall-normal,
    population, terrain (elevation/slope), flood susceptibility, + a null pm25_obs label column
  • output = newline-delimited JSON (OUT_FORMAT=ndjson) — universally loadable into DuckDB/pandas/pyarrow
  M3 EXTENDS this same seam: OUT_FORMAT=parquet, temporal lags, H3-neighbour aggregates, wind-aware transport
  features, and a joined pm25_obs label from CPCB stations (needs the Delhi CPCB key).

NO SYNTHETIC FILL — a cell carries `null` for any feature whose source fact is absent; missingness is explicit
(ADR-0073 §2). REAL registry facts only; if cell.cams is empty for the tenant the engine writes nothing and says so.

  Env: TENANT · AOI_ID (opt; else discover) · HOUR (opt) · OUT_FORMAT (ndjson) · RUN_TOKEN · AIRAWAT_API
"""
import base64, json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_HINT = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
HOUR = os.environ.get("HOUR", "").strip()
# MODE: 'snapshot' (default) = one row per cell at a single target hour (inference-ready grid).
#       'train'              = one row per LABELLED (station, hour) with features aligned to THAT hour — the
#                              supervised TRAINING panel (ADR-0073 M3.6, needs historical feature series from the
#                              feature-backfill driver). Same columns + same materialise/manifest seam.
MODE = os.environ.get("MODE", "snapshot").strip().lower()
OUT_FORMAT = os.environ.get("OUT_FORMAT", "parquet").strip().lower()   # M3: Parquet by default (pyarrow in the feature runtime)
NBR_K = int(os.environ.get("NBR_K", "1"))                              # H3 k-ring radius for neighbour-context features
CID = os.environ.get("AIROS_COMPONENT_ID", "ds:airawatdeveloper:engine/pm25-features")
CVER = os.environ.get("AIROS_COMPONENT_VERSION", "0.1.0")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

# each source scope → the columns it contributes, as (fact_field, matrix_column).
FEATURE_SOURCES = {
    "cell.cams":           [("pm25", "pm25_cams"), ("pm10", "pm10_cams"), ("no2", "no2_cams"),
                            ("o3", "o3_cams"), ("so2", "so2_cams")],
    "cell.boundary_layer": [("pblh_m", "pblh_m")],
    "cell.aod":            [("aod", "aod"), ("valid_fraction", "aod_valid_fraction")],
    "cell.rainfall":       [("rainfall_mm", "rainfall_mm")],
    "demographics.population": [("population", "population"), ("people", "population"), ("value", "population")],  # tolerate field name
    "geo.terrain":         [("elevation_m", "elevation_m"), ("slope_deg", "slope_deg")],
    "cell.hydrology":      [("flood_susceptibility", "flood_susceptibility"), ("hand_mean_m", "hand_mean_m")],
    # Anthropogenic-emission + land-type proxies (M3.4): built density = traffic/combustion source strength;
    # built-up vs green fraction = surface type. Strong static predictors of the LOCAL PM2.5 add-on over the
    # regional background (the CAMS term). All already ingested as governed baseline layers.
    "cell.buildings":      [("built_area_m2", "built_area_m2"), ("building_coverage", "building_coverage"),
                            ("mean_height_m", "building_mean_height_m"), ("built_volume_m3", "built_volume_m3")],
    "cell.builtup":        [("value", "builtup_frac")],
    "cell.greencover":     [("value", "greencover_frac")],
    "econ.nightlight":     [("avg_radiance", "nightlight_radiance")],   # night-time lights = combustion/economic-activity proxy
    "cell.roads":          [("road_length_m", "road_length_m"), ("road_density_km_per_km2", "road_density")],  # traffic-emission proxy
}
FEATURE_COLS = ["pm25_cams", "pm10_cams", "no2_cams", "o3_cams", "so2_cams", "pblh_m", "aod", "aod_valid_fraction",
                "rainfall_mm", "population", "elevation_m", "slope_deg", "flood_susceptibility", "hand_mean_m",
                "built_area_m2", "building_coverage", "building_mean_height_m", "built_volume_m3",
                "builtup_frac", "greencover_frac", "nightlight_radiance", "road_length_m", "road_density"]
# H3-neighbour context: the k-ring mean of these fields (a cell's regional/local surroundings — pollution is
# spatially autocorrelated, so neighbour means are strong features and smooth over per-cell retrieval gaps).
NBR_BASE = ["pm25_cams", "no2_cams", "pblh_m", "aod"]
NBR_COLS = [c + "_nbr" for c in NBR_BASE]
# Temporal lags (M3): assembled from the coarse `/series/` anchor facts the cams/pblh drivers write at the sample
# points. Each target cell inherits its NEAREST anchor's lagged value (CAMS/PBLH are coarse → lags are regional).
LAGS_PM, LAGS_PBL = [1, 3, 6, 24], [3, 24]
LAG_COLS = [f"pm25_cams_lag{k}" for k in LAGS_PM] + [f"pblh_m_lag{k}" for k in LAGS_PBL]


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def unwrap(res):
    if isinstance(res, dict) and "object" in res and isinstance(res["object"], dict):
        return res["object"]
    return res


def resolve_aoi():
    if AOI_HINT:
        o = unwrap(call("/v1/read", {"scope": "space.aoi", "id": AOI_HINT}))
        if isinstance(o, dict) and len(o.get("bbox") or []) == 4:
            return AOI_HINT
    rows = call("/v1/query", {"scope": "space.aoi"}).get("results") or []
    mine = [(r.get("object") or r) for r in rows
            if str((r.get("object") or r).get("id", "")).startswith(f"ds:{TENANT}:")
            and len((r.get("object") or r).get("bbox") or []) == 4]
    if not mine:
        raise SystemExit(f"FATAL: no space.aoi for tenant '{TENANT}'.")
    return max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))["id"]


def _rows(scope):
    try:
        return [(r.get("object") or r) for r in (call("/v1/query", {"scope": scope}).get("results") or [])]
    except Exception as ex:
        print(f"  (read {scope} skipped: {type(ex).__name__})", flush=True)
        return []


def _h3(o):
    return (o.get("cell_ref") or o.get("space") or {}).get("h3_id") or (o.get("space") or {}).get("h3")


def _materialise(rows, cols):
    """Encode rows → filestore blob (Parquet if pyarrow, else ndjson). Returns (file_id, ext, size)."""
    if OUT_FORMAT == "parquet":
        try:
            import pyarrow as pa, pyarrow.parquet as pq, io
            table = pa.table({c: [r.get(c) for r in rows] for c in cols})
            buf = io.BytesIO(); pq.write_table(table, buf); blob = buf.getvalue(); ext = "parquet"
        except ImportError:
            blob = ("\n".join(json.dumps(r) for r in rows)).encode(); ext = "ndjson"
    else:
        blob = ("\n".join(json.dumps(r) for r in rows)).encode(); ext = "ndjson"
    put = call("/v1/file/put", {"content_b64": base64.b64encode(blob).decode()})
    return put.get("id"), ext, put.get("size")


def _series_by_cell_hour(scope, fields):
    """{(h3, 'YYYY-MM-DDTHH'): {col: value}} from the `/series/` facts of a scope (feature-backfill / cams / pblh)."""
    out = {}
    for o in _rows(scope):
        if "/series/" not in str(o.get("id", "")):
            continue
        h = _h3(o)
        ts = o.get("observed_at")
        if not h or not ts:
            continue
        out[(h, str(ts)[:13])] = {col: o.get(ff) for ff, col in fields}
    return out


def _static_by_cell():
    """{h3: {matrix_col: value}} for the STATIC (time-invariant) baseline layers — one value per cell."""
    static_scopes = ["cell.rainfall", "demographics.population", "geo.terrain", "cell.hydrology",
                     "cell.buildings", "cell.builtup", "cell.greencover", "econ.nightlight", "cell.roads"]
    by = {}
    for scope in static_scopes:
        for o in _rows(scope):
            if "/series/" in str(o.get("id", "")):
                continue
            h = _h3(o)
            if not h:
                continue
            d = by.setdefault(h, {})
            for ff, col in FEATURE_SOURCES.get(scope, []):
                v = o.get(ff)
                if v is not None and d.get(col) is None:
                    d[col] = v
    return by


def build_training_matrix():
    """M3.6 TRAIN mode: one row per LABELLED (station, hour), features aligned to that exact hour.
    Hourly features come from the cell.cams/cell.aod/cell.boundary_layer `/series/` facts (the feature-backfill
    driver writes these historically at the label cells); static layers join per-cell; lags read the same series
    at hour−k. This is the supervised panel the pm25-model trains + validates (leave-station-out) on."""
    import datetime as _dt
    aoi_ref = resolve_aoi()
    labels = [o for o in _rows("air.sensor_reading") if isinstance(o.get("pm25") or o.get("value"), (int, float))]
    if not labels:
        raise SystemExit("FATAL: no air.sensor_reading labels — run air-sensors / openaq-archive first.")
    cams_s = _series_by_cell_hour("cell.cams", [("pm25", "pm25_cams"), ("pm10", "pm10_cams"),
                                                ("no2", "no2_cams"), ("o3", "o3_cams"), ("so2", "so2_cams")])
    aod_s = _series_by_cell_hour("cell.aod", [("aod", "aod")])
    pbl_s = _series_by_cell_hour("cell.boundary_layer", [("pblh_m", "pblh_m")])
    static = _static_by_cell()
    print(f"tenant={TENANT} aoi={aoi_ref} · MODE=train · {len(labels)} labels · "
          f"series: cams={len(cams_s)} aod={len(aod_s)} pblh={len(pbl_s)} · static cells={len(static)}", flush=True)

    rows, aligned = [], 0
    for o in labels:
        h = _h3(o) or o.get("h3_id")
        ts = o.get("observed_at")
        if not h or not ts:
            continue
        hk = str(ts)[:13]
        sp = o.get("cell_ref") or o.get("space") or {}
        cen = sp.get("centroid") or [None, None]
        row = {"h3_id": h, "resolution": sp.get("resolution") or sp.get("res"),
               "lon": cen[0], "lat": cen[1], "observed_at": ts,
               "pm25_obs": o.get("pm25") if isinstance(o.get("pm25"), (int, float)) else o.get("value")}
        for c in FEATURE_COLS + NBR_COLS + LAG_COLS:
            row[c] = None
        # hourly features aligned to THIS label's hour
        hr = cams_s.get((h, hk)) or {}
        for c, v in hr.items():
            row[c] = v
        if (h, hk) in aod_s:
            row["aod"] = aod_s[(h, hk)].get("aod")
        if (h, hk) in pbl_s:
            row["pblh_m"] = pbl_s[(h, hk)].get("pblh_m")
        # static per-cell features
        for c, v in (static.get(h) or {}).items():
            row[c] = v
        # temporal lags from the same-cell series at hour−k
        try:
            t0 = _dt.datetime.strptime(hk, "%Y-%m-%dT%H")
            for k in LAGS_PM:
                key = (t0 - _dt.timedelta(hours=k)).strftime("%Y-%m-%dT%H")
                row[f"pm25_cams_lag{k}"] = (cams_s.get((h, key)) or {}).get("pm25_cams")
            for k in LAGS_PBL:
                key = (t0 - _dt.timedelta(hours=k)).strftime("%Y-%m-%dT%H")
                row[f"pblh_m_lag{k}"] = (pbl_s.get((h, key)) or {}).get("pblh_m")
        except Exception:
            pass
        if row.get("pm25_cams") is not None:
            aligned += 1
        rows.append(row)

    # H3-neighbour means over the labelled cells present at the SAME hour (usually 1 station → null; honest).
    try:
        import h3 as _h3lib
        byhour = {}
        for r in rows:
            byhour.setdefault(str(r["observed_at"])[:13], {})[r["h3_id"]] = r
        for r in rows:
            peers = byhour.get(str(r["observed_at"])[:13], {})
            try:
                ring = set(_h3lib.grid_disk(r["h3_id"], NBR_K))
            except Exception:
                ring = {r["h3_id"]}
            for base, col in zip(NBR_BASE, NBR_COLS):
                vals = [peers[nb][base] for nb in ring if nb in peers and isinstance(peers[nb].get(base), (int, float))]
                r[col] = round(sum(vals) / len(vals), 3) if vals else None
    except Exception as ex:
        print(f"  (neighbour aggregates skipped: {type(ex).__name__})", flush=True)

    obs_hours = sorted({str(r["observed_at"]) for r in rows})
    target = obs_hours[-1] if obs_hours else NOW
    cols = ["h3_id", "resolution", "lon", "lat", "observed_at"] + FEATURE_COLS + NBR_COLS + LAG_COLS + ["pm25_obs"]
    file_id, ext, size = _materialise(rows, cols)
    print(f"materialised pm25 TRAIN panel: {len(rows)} labelled station-hours "
          f"({aligned} with aligned CAMS features) × {len(FEATURE_COLS)+len(NBR_COLS)+len(LAG_COLS)} features "
          f"→ filestore {file_id} ({ext}, {size} bytes)", flush=True)

    win = f"{obs_hours[0][:10]}_{obs_hours[-1][:10]}".replace("-", "") if obs_hours else "window"
    fm_id = f"ds:{TENANT}:air/feature_matrix/pm25-train/{win}"
    fm = {"schema": "air.feature_matrix", "id": fm_id, "feature_set": "pm25-train", "aoi": aoi_ref,
          "file_id": file_id, "format": ext, "rows": len(rows),
          "n_features": len(FEATURE_COLS) + len(NBR_COLS) + len(LAG_COLS), "columns": cols,
          "feature_cols": FEATURE_COLS + NBR_COLS + LAG_COLS, "label_col": "pm25_obs",
          "labels_present": len(rows), "size_bytes": size, "method": "pm25-features-train",
          "observed_at": target, "built_at": NOW, "prov": {"component": f"{CID}@{CVER}", "built_at": NOW}}
    call("/v1/write", {"scope": "air.feature_matrix", "id": fm_id, "object": fm,
                       "valid_time": {"from": target}, "asserted_at": NOW})
    print(f"  wrote air.feature_matrix manifest {fm_id}", flush=True)
    print(f"RESULT: {json.dumps({k: fm[k] for k in ('rows','labels_present','n_features','file_id','format','feature_set')} | {'aligned_feature_rows': aligned})}")
    return 0


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — an engine runs in exactly one tenant.")
    if MODE == "train":
        return build_training_matrix()
    aoi_ref = resolve_aoi()

    # 1) the CAMS facts define the cell set + the target hour (the nowcast timestep).
    cams = _rows("cell.cams")
    if not cams:
        raise SystemExit("FATAL: no cell.cams facts for this tenant — run the cams-regional driver first (nothing to assemble).")
    target = HOUR or max((o.get("observed_at") or "") for o in cams)
    cams_h = {_h3(o): o for o in cams if o.get("observed_at") == target and _h3(o)}
    print(f"tenant={TENANT} aoi={aoi_ref} · target hour {target} · {len(cams_h)} cells (from cell.cams)", flush=True)

    # 2) seed one row per cell from the CAMS facts (h3, geometry, target hour).
    matrix = {}
    for h, o in cams_h.items():
        cr = o.get("cell_ref") or {}
        cen = cr.get("centroid") or [None, None]
        matrix[h] = {"h3_id": h, "resolution": cr.get("resolution"), "lon": cen[0], "lat": cen[1],
                     "observed_at": target, "pm25_obs": None}
        for col in FEATURE_COLS:
            matrix[h][col] = None

    # 3) enrich each row with every other source, joined on h3. For the two HOURLY sources match the target hour;
    #    the static baseline layers (population/terrain/hydrology/rainfall-normal) join regardless of time.
    hourly = {"cell.cams", "cell.boundary_layer"}
    filled = {}
    for scope, fields in FEATURE_SOURCES.items():
        n = 0
        for o in _rows(scope):
            h = _h3(o)
            if h is None or h not in matrix:
                continue
            if scope in hourly and o.get("observed_at") != target:
                continue
            for fact_field, col in fields:
                v = o.get(fact_field)
                if v is not None and matrix[h].get(col) is None:
                    matrix[h][col] = v
                    n += 1
        filled[scope] = n
        print(f"  joined {scope}: {n} cell-values", flush=True)

    # 4) ground-truth label (air.sensor_reading) — point observations assigned to their H3 cell at the target
    #    hour when present. Delhi CPCB ingest needs a key (ADR-0073), so this is usually 0 today — the label
    #    column stays null and the matrix is inference-ready but not yet trainable. Honest, not fabricated.
    obs = _rows("air.sensor_reading")
    labelled = 0
    for o in obs:
        h = _h3(o) or o.get("h3_id")
        val = o.get("pm25") or o.get("value")
        if h in matrix and isinstance(val, (int, float)):
            if matrix[h]["pm25_obs"] is None:
                matrix[h]["pm25_obs"] = val
                labelled += 1
    print(f"  ground-truth labels (air.sensor_reading @ {target}): {labelled} cells", flush=True)

    # 4b) H3-neighbour aggregates (M3): each cell's k-ring mean of the key fields. Pure in-memory compute over
    #     the matrix (no extra registry reads) — pollution is spatially autocorrelated, so a neighbour mean is a
    #     strong feature and also smooths over cells that missed a per-cell satellite/regional retrieval.
    for h, row in matrix.items():
        try:
            ring = h3.grid_disk(h, NBR_K)
        except Exception:
            ring = [h]
        for base, col in zip(NBR_BASE, NBR_COLS):
            vals = [matrix[nb][base] for nb in ring if nb in matrix and isinstance(matrix[nb].get(base), (int, float))]
            row[col] = round(sum(vals) / len(vals), 3) if vals else None
    print(f"  H3-neighbour aggregates (k={NBR_K}): {len(NBR_COLS)} columns over {len(matrix)} cells", flush=True)

    # 4c) temporal lags from the coarse point-series anchors (M3). The cams/pblh drivers write an hourly series at
    #     the ~few dozen sample cells under `/series/` ids; here each target cell inherits its NEAREST anchor's
    #     lagged values. Missing hours stay null (honest — no interpolation across gaps).
    import datetime as _dt
    def _hk(ts): return str(ts)[:13]
    def _anchors(scope, fields):
        A = {}
        for o in _rows(scope):
            if "/series/" not in str(o.get("id", "")):
                continue
            h = _h3(o)
            if not h:
                continue
            cr = o.get("cell_ref") or {}; cen = cr.get("centroid") or [None, None]
            a = A.setdefault(h, {"cen": cen, "s": {}})
            a["s"][_hk(o.get("observed_at"))] = {c: o.get(f) for f, c in fields}
        return A
    cams_anch = _anchors("cell.cams", [("pm25", "pm25")])
    pbl_anch = _anchors("cell.boundary_layer", [("pblh_m", "pblh_m")])
    try:
        tgt = _dt.datetime.strptime(_hk(target), "%Y-%m-%dT%H")
    except Exception:
        tgt = None

    def _nearest(anchors, lon, lat):
        best, bd = None, 1e18
        if lon is None or lat is None:
            return None
        for a in anchors.values():
            c = a["cen"]
            if not c or c[0] is None:
                continue
            d = (c[0] - lon) ** 2 + (c[1] - lat) ** 2
            if d < bd:
                bd, best = d, a
        return best

    if tgt and (cams_anch or pbl_anch):
        for h, row in matrix.items():
            ca = _nearest(cams_anch, row.get("lon"), row.get("lat"))
            pa = _nearest(pbl_anch, row.get("lon"), row.get("lat"))
            for k in LAGS_PM:
                key = (tgt - _dt.timedelta(hours=k)).strftime("%Y-%m-%dT%H")
                row[f"pm25_cams_lag{k}"] = (ca["s"].get(key) or {}).get("pm25") if ca else None
            for k in LAGS_PBL:
                key = (tgt - _dt.timedelta(hours=k)).strftime("%Y-%m-%dT%H")
                row[f"pblh_m_lag{k}"] = (pa["s"].get(key) or {}).get("pblh_m") if pa else None
        print(f"  temporal lags: {len(LAG_COLS)} cols from {len(cams_anch)} cams + {len(pbl_anch)} pblh series anchors", flush=True)
    else:
        for row in matrix.values():
            for c in LAG_COLS:
                row[c] = None
        print("  temporal lags: no /series/ anchors yet (run cams/pblh ≥0.2.0 for history) — lag cols null", flush=True)

    # 5) materialise → filestore. M1 = ndjson; M3 = parquet (pyarrow) — same seam, swapped encoder.
    rows = list(matrix.values())
    if OUT_FORMAT == "parquet":
        try:
            import pyarrow as pa, pyarrow.parquet as pq, io
            cols = ["h3_id", "resolution", "lon", "lat", "observed_at"] + FEATURE_COLS + NBR_COLS + LAG_COLS + ["pm25_obs"]
            table = pa.table({c: [r.get(c) for r in rows] for c in cols})
            buf = io.BytesIO(); pq.write_table(table, buf); blob = buf.getvalue(); ext = "parquet"
        except ImportError:
            print("  (pyarrow unavailable — falling back to ndjson; add pyarrow to the feature runtime for M3)", flush=True)
            blob = ("\n".join(json.dumps(r) for r in rows)).encode(); ext = "ndjson"
    else:
        blob = ("\n".join(json.dumps(r) for r in rows)).encode(); ext = "ndjson"

    put = call("/v1/file/put", {"content_b64": base64.b64encode(blob).decode()})
    file_id = put.get("id")

    manifest = {"feature_set": "pm25", "tenant": TENANT, "aoi": aoi_ref, "observed_at": target,
                "rows": len(rows), "columns": ["h3_id", "resolution", "lon", "lat", "observed_at"] + FEATURE_COLS + NBR_COLS + LAG_COLS + ["pm25_obs"],
                "feature_cols": FEATURE_COLS + NBR_COLS + LAG_COLS, "label_col": "pm25_obs", "labels_present": labelled,
                "sources_joined": filled, "format": ext, "size_bytes": put.get("size"),
                "file_id": file_id, "prov": {"component": f"{CID}@{CVER}", "built_at": NOW},
                "built_at": NOW}
    print(f"materialised pm25 feature matrix: {len(rows)} rows × {len(FEATURE_COLS) + len(NBR_COLS) + len(LAG_COLS)} features → filestore {file_id} ({ext}, {put.get('size')} bytes)", flush=True)

    # governed manifest (air.feature_matrix): the catalog entry that points at the blob, so model engines read ONE
    # Parquet instead of rescanning raw scopes. id keyed by hour → each materialisation is retained; readers take max.
    hourkey = str(target).replace("-", "").replace(":", "").replace("T", "")[:10]
    fm_id = f"ds:{TENANT}:air/feature_matrix/pm25/{hourkey}"
    all_cols = ["h3_id", "resolution", "lon", "lat", "observed_at"] + FEATURE_COLS + NBR_COLS + LAG_COLS + ["pm25_obs"]
    fm = {"schema": "air.feature_matrix", "id": fm_id, "feature_set": "pm25", "aoi": aoi_ref,
          "file_id": file_id, "format": ext, "rows": len(rows), "n_features": len(FEATURE_COLS) + len(NBR_COLS) + len(LAG_COLS),
          "columns": all_cols, "feature_cols": FEATURE_COLS + NBR_COLS + LAG_COLS, "label_col": "pm25_obs",
          "labels_present": labelled, "size_bytes": put.get("size"), "method": "pm25-features",
          "observed_at": target, "built_at": NOW, "prov": {"component": f"{CID}@{CVER}", "built_at": NOW}}
    call("/v1/write", {"scope": "air.feature_matrix", "id": fm_id, "object": fm,
                       "valid_time": {"from": target}, "asserted_at": NOW})
    print(f"  wrote air.feature_matrix manifest {fm_id}", flush=True)

    print(f"RESULT: {json.dumps(manifest)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
