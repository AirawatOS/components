#!/usr/bin/env python3
"""pm25-model — PM2.5 model trainer + evaluator (ADR-0073 M4).

Reads the MATERIALISED feature store — the `air.feature_matrix` manifest points at the Parquet blob the
`pm25-features` engine built — so this engine fetches ONE columnar file (the full 33-feature `(h3×hour)` matrix,
incl. temporal lags + H3-neighbour context) instead of rescanning a dozen raw registry scopes. It trains the ADR's
model sequence on the rows that carry a CPCB label (`pm25_obs`), and validates with LEAVE-ONE-STATION-OUT
cross-validation — the acceptance criterion: how well does it predict an UNSEEN monitor?

  • baseline_cams — predict = regional CAMS background (pm25_cams): the physical null model everything must beat.
  • baseline_mean — predict = training mean (trivial null).
  • linear (Ridge) — regularised linear over standardised features.
  • random_forest — the benchmark.
  • xgboost       — the primary model; exact TreeSHAP contributors via pred_contribs (no external shap lib).

REAL facts only — no synthetic labels; too few labelled stations → it says so rather than inventing data.
M4.1 = train + evaluate (metrics in RESULT); full-grid inference → air.pm25_estimate is the next slice.

  Env: TENANT · AOI_ID (opt) · MATRIX_FILE_ID (opt override) · RUN_TOKEN · AIRAWAT_API
"""
import base64, io, json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
MATRIX_FILE_ID = os.environ.get("MATRIX_FILE_ID", "").strip()
FEATURE_SET = os.environ.get("FEATURE_SET", "pm25").strip()   # e.g. "pm25-train-cpcb" for the 3-yr fleet corpus
CV_FOLDS = int(os.environ.get("CV_FOLDS", "5"))
HOLDOUT_YEAR = os.environ.get("HOLDOUT_YEAR", "2025").strip() # temporal holdout: train < year, test == year
SHAP_SAMPLE = int(os.environ.get("SHAP_SAMPLE", "5000"))
INFER = os.environ.get("INFER", "").strip() in ("1", "true", "yes")   # full-grid inference → air.quality surface
PERSIST = os.environ.get("PERSIST", "").strip() in ("1", "true", "yes")  # TRAIN the deployable model + persist a champion air.model (weekly)
MODEL_FILE_ID = os.environ.get("MODEL_FILE_ID", "").strip()           # INFER: pin a specific serialised model instead of the champion
TRAIN_IF_MISSING = os.environ.get("TRAIN_IF_MISSING", "1").strip() in ("1", "true", "yes")  # INFER bootstrap: if no champion yet, train+persist one, then load
MIN_R2 = float(os.environ.get("MIN_R2", "0.3"))          # champion ACCEPTANCE floor: a challenger below this leave-station-out r2 never promotes
MIN_IMPROVE = float(os.environ.get("MIN_IMPROVE", "0.0"))  # challenger must reach incumbent_r2 + this to promote (raise to damp churn / force a clear win)
SNAPSHOT_FEATURE_SET = os.environ.get("SNAPSHOT_FEATURE_SET", "pm25").strip()   # the (h3×latest-hour) grid matrix
OUT_SCHEMA = os.environ.get("OUT_SCHEMA", "air.quality").strip()      # governed cell-PM2.5 estimate (has a viz.layer)
MODEL_SCHEMA = "air.model"                                            # governed champion-model manifest (points at the filestore blob)
SCENARIO = os.environ.get("SCENARIO", "").strip()                     # optional: freeze this run as a named scenario snapshot (own id namespace)
CLIP_MAX = float(os.environ.get("CLIP_MAX", "985"))                   # clip predictions (CPCB caps ~1000; drop the rail)
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
LABEL = "pm25_obs"


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def _rows(scope):
    return [(r.get("object") or r) for r in (call("/v1/query", {"scope": scope}).get("results") or [])]


def _load_matrix(feature_set=None, allow_file_id=True, pin_file_id=""):
    """Resolve the air.feature_matrix manifest (given feature_set) → fetch its blob → (data, feature_cols, meta).
    pin_file_id targets a SPECIFIC snapshot blob (e.g. a winter-hour grid) instead of the latest by observed_at."""
    fs = feature_set or FEATURE_SET
    fid, fmt, fcols, obs = (pin_file_id or (MATRIX_FILE_ID if allow_file_id else "")), "parquet", None, None
    if pin_file_id:
        mans = [m for m in _rows("air.feature_matrix") if m.get("file_id") == pin_file_id]
        if mans:
            m = mans[0]; fmt, fcols, obs = m.get("format", "parquet"), m.get("feature_cols"), m.get("observed_at")
    if not fid:
        mans = [m for m in _rows("air.feature_matrix") if m.get("feature_set") == fs and m.get("file_id")]
        if not mans:
            raise SystemExit(f"FATAL: no air.feature_matrix (feature_set={fs}) manifest — run the builder first.")
        man = max(mans, key=lambda m: m.get("observed_at") or "")
        fid, fmt, fcols, obs = man["file_id"], man.get("format", "parquet"), man.get("feature_cols"), man.get("observed_at")
    blob = base64.b64decode(call("/v1/file/get", {"id": fid}).get("content_b64") or "")
    if not blob:
        raise SystemExit(f"FATAL: feature-matrix blob {fid} empty / not in filestore.")
    if fmt == "parquet":
        import pyarrow.parquet as pq
        tbl = pq.read_table(io.BytesIO(blob))
        data = tbl.to_pydict()                              # COLUMNAR — never build N per-row dicts (N can be ~1M)
    else:
        rows = [json.loads(ln) for ln in blob.decode().splitlines() if ln.strip()]
        data = {c: [r.get(c) for r in rows] for c in (rows[0].keys() if rows else [])}
    keys = list(data.keys())
    if not fcols:                                            # fall back: infer feature cols (numeric, minus id/geo/label)
        skip = {"h3_id", "resolution", "lon", "lat", "observed_at", "station", LABEL}
        fcols = [c for c in keys if c not in skip]
    return data, fcols, {"file_id": fid, "format": fmt, "observed_at": obs}


def _put_file(raw: bytes) -> str:
    """Upload bytes to the content-addressed filestore via the broker → the sha2 file id."""
    r = call("/v1/file/put", {"content_b64": base64.b64encode(raw).decode()})
    fid = r.get("id") or r.get("file_id")
    if not fid:
        raise SystemExit(f"FATAL: filestore put returned no id: {r}")
    return fid


def _num_np(np):
    def num(v):
        try:
            f = float(v); return f if f == f else np.nan
        except (TypeError, ValueError):
            return np.nan
    return num


def _deployable_xy(np, corpus, cfeat, sfeat):
    """DEPLOYABLE training set: labelled corpus rows × the feature cols that ALSO exist in the grid snapshot
    (train/infer parity — no station-only weather the grid can't supply). Returns (feats, X, y, groups)."""
    feats = [c for c in sfeat if c in cfeat]                               # grid-deployable intersection, snapshot order
    if not feats:
        raise SystemExit("FATAL: no shared features between corpus and snapshot — cannot train a deployable model.")
    num = _num_np(np)
    ycol = corpus.get(LABEL) or []
    ci = [i for i in range(len(ycol)) if isinstance(ycol[i], (int, float))]
    MAX_TRAIN = int(os.environ.get("MAX_TRAIN", "200000"))
    if MAX_TRAIN and len(ci) > MAX_TRAIN:
        ci = sorted(np.random.default_rng(0).choice(np.array(ci), size=MAX_TRAIN, replace=False).tolist())
    cv = {c: corpus.get(c) or [] for c in feats}
    X = np.array([[num(cv[c][i]) for c in feats] for i in ci], dtype=float)
    y = np.array([num(ycol[i]) for i in ci], dtype=float)
    gcol = corpus.get("station") or corpus.get("h3_id") or [None] * len(ycol)
    groups = np.array([gcol[i] for i in ci])
    return feats, X, y, groups


def _incumbent():
    """The current CHAMPION air.model for FEATURE_SET (the stable id, champion:true), or None."""
    mid = f"ds:{TENANT}:air/model/{FEATURE_SET}"
    hit = [m for m in _rows(MODEL_SCHEMA) if m.get("id") == mid and m.get("champion") and m.get("file_id")]
    return hit[0] if hit else None


def persist_model():
    """TRAIN a CHALLENGER (deployable, grid-available features) on the CPCB corpus, score its leave-station-out
    skill, and GATE promotion: it becomes the champion (the STABLE air.model id, supersede-in-place) ONLY if it
    clears the absolute acceptance floor (MIN_R2) AND matches-or-beats the incumbent by MIN_IMPROVE. Otherwise it
    is recorded as a REJECTED challenger (champion:false, its OWN id) for audit and the incumbent stays — so the
    hourly nowcast keeps loading the proven model. Weekly job."""
    import numpy as np, xgboost as xgb, tempfile
    corpus, cfeat, cmeta = _load_matrix(FEATURE_SET)
    snap, sfeat, _ = _load_matrix(SNAPSHOT_FEATURE_SET, allow_file_id=False,
                                  pin_file_id=os.environ.get("SNAPSHOT_FILE_ID", "").strip())
    feats, X, y, groups = _deployable_xy(np, corpus, cfeat, sfeat)
    n_st = len(set(groups.tolist()))
    print(f"PERSIST: train CHALLENGER on {len(y)} labelled rows × {len(feats)} grid features ({n_st} stations)", flush=True)
    metrics = {}
    try:                                                                   # challenger's OWN leave-station-out skill
        from sklearn.model_selection import GroupKFold, cross_val_predict
        folds = max(2, min(int(os.environ.get("CV_FOLDS", "3")), n_st))
        yp = cross_val_predict(xgb.XGBRegressor(n_estimators=200, max_depth=6, learning_rate=0.05, subsample=0.8,
                               colsample_bytree=0.8, reg_lambda=1.0, tree_method="hist", random_state=0),
                               X, y, cv=GroupKFold(n_splits=folds), groups=groups, n_jobs=1)
        err = yp - y; ss_res = float(np.sum(err ** 2)); ss_tot = float(np.sum((y - y.mean()) ** 2))
        metrics = {"leave_station_out": {"rmse": round(float(np.sqrt(np.mean(err ** 2))), 2),
                                         "mae": round(float(np.mean(np.abs(err))), 2),
                                         "r2": round(1 - ss_res / ss_tot, 3) if ss_tot > 0 else None, "folds": folds}}
        print(f"  [LSO] challenger: {metrics['leave_station_out']}", flush=True)
    except Exception as e:
        print(f"  (skipped LSO metric: {e})", flush=True)
    model = xgb.XGBRegressor(n_estimators=400, max_depth=6, learning_rate=0.05, subsample=0.8,
                             colsample_bytree=0.8, reg_lambda=1.0, tree_method="hist", random_state=0)
    model.fit(X, y)
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    model.save_model(tmp); raw = open(tmp, "rb").read()
    fid = _put_file(raw)
    r2 = (metrics.get("leave_station_out") or {}).get("r2")

    # ---- CHAMPION / CHALLENGER GATE -----------------------------------------------------------------
    inc = _incumbent()
    inc_r2 = ((inc or {}).get("metrics") or {}).get("leave_station_out", {}).get("r2")
    floor_ok = isinstance(r2, (int, float)) and r2 >= MIN_R2
    beats = (inc is None) or not isinstance(inc_r2, (int, float)) or (r2 is not None and r2 >= inc_r2 + MIN_IMPROVE)
    promote = bool(floor_ok and beats)
    verdict = ("promoted" if promote else ("rejected:below-floor(%.3f<%.2f)" % (r2 if isinstance(r2, (int, float)) else -9, MIN_R2)
               if not floor_ok else "rejected:not-better-than-incumbent(%.3f<%.3f+%.3f)" % (r2, inc_r2, MIN_IMPROVE)))
    ts = NOW.replace("-", "").replace(":", "").replace("T", "")[:12]
    champ_id = f"ds:{TENANT}:air/model/{FEATURE_SET}"                       # the STABLE champion slot (what INFER loads)
    mid = champ_id if promote else f"ds:{TENANT}:air/model/{FEATURE_SET}/challenger/{ts}"
    obj = {"schema": MODEL_SCHEMA, "id": mid, "model_family": "pm25", "feature_set": FEATURE_SET,
           "algo": "xgboost", "format": "xgboost-json", "file_id": fid, "feature_cols": feats,
           "hyperparams": {"n_estimators": 400, "max_depth": 6, "learning_rate": 0.05},
           "metrics": metrics, "trained_on": cmeta["file_id"], "n_rows": int(len(y)), "n_stations": n_st,
           "validation": "leave-station-out (GroupKFold)", "champion": promote, "observed_at": NOW,
           "verdict": verdict, "incumbent": (inc or {}).get("id"), "incumbent_r2": inc_r2,
           "gate": {"min_r2": MIN_R2, "min_improve": MIN_IMPROVE, "floor_ok": floor_ok, "beats_incumbent": bool(beats)},
           "prov": {"component": "ds:airawatdeveloper:engine/pm25-model@0.6.0", "built_at": NOW,
                    "trained_on": cmeta["file_id"], "feature_set": FEATURE_SET, "r2": r2, "verdict": verdict}}
    call("/v1/write", {"scope": MODEL_SCHEMA, "id": mid, "object": obj, "valid_time": {"from": NOW}, "asserted_at": NOW})
    tag = "PROMOTED → champion" if promote else "REJECTED (incumbent kept)"
    print(f"GATE {tag}: challenger r2={r2} vs incumbent r2={inc_r2} (floor {MIN_R2}, +{MIN_IMPROVE}) · wrote {mid}", flush=True)
    print(f"RESULT: {json.dumps({'persist': True, 'promoted': promote, 'verdict': verdict, 'model_id': mid, 'champion_id': champ_id, 'file_id': fid, 'r2': r2, 'incumbent_r2': inc_r2, 'features': len(feats), 'n_rows': int(len(y)), 'n_stations': n_st})}")
    return {"file_id": fid, "feature_cols": feats, "promoted": promote}


def _load_champion():
    """Resolve the CHAMPION air.model for FEATURE_SET (or a pinned MODEL_FILE_ID) → (model, feature_cols, manifest).
    Loads the STABLE champion slot / champion:true ONLY — a rejected challenger (its own id, champion:false) is
    never loaded even though it may be newer, so the gate's decision actually governs what serves."""
    import xgboost as xgb, tempfile
    man = None
    if MODEL_FILE_ID:
        hit = [m for m in _rows(MODEL_SCHEMA) if m.get("file_id") == MODEL_FILE_ID]
        man = hit[0] if hit else {"file_id": MODEL_FILE_ID, "feature_cols": None}
    else:
        champ_id = f"ds:{TENANT}:air/model/{FEATURE_SET}"
        hit = [m for m in _rows(MODEL_SCHEMA) if m.get("id") == champ_id and m.get("champion") and m.get("file_id")]
        if hit:
            man = hit[0]
        else:                                                              # fallback: newest champion:true for this set
            cands = [m for m in _rows(MODEL_SCHEMA) if m.get("feature_set") == FEATURE_SET and m.get("champion") and m.get("file_id")]
            if cands:
                man = max(cands, key=lambda m: m.get("observed_at") or "")
    if not man:
        return None
    raw = base64.b64decode(call("/v1/file/get", {"id": man["file_id"]}).get("content_b64") or "")
    if not raw:
        return None
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    open(tmp, "wb").write(raw)
    model = xgb.XGBRegressor(); model.load_model(tmp)
    return model, man.get("feature_cols"), man


def infer_and_write():
    """LOAD the champion air.model (NO retraining) + PREDICT every AOI cell from the latest pm25-features snapshot
    → one air.quality estimate per cell. If no champion exists yet and TRAIN_IF_MISSING, bootstrap one first."""
    import numpy as np
    loaded = _load_champion()
    if loaded is None:
        if not TRAIN_IF_MISSING:
            raise SystemExit(f"FATAL: no champion air.model (feature_set={FEATURE_SET}) and TRAIN_IF_MISSING=0.")
        print("INFER: no champion yet — bootstrapping one via persist_model()…", flush=True)
        persist_model()
        loaded = _load_champion()
        if loaded is None:
            raise SystemExit("FATAL: bootstrap train produced no loadable champion.")
    model, feats, man = loaded
    snap, sfeat, smeta = _load_matrix(SNAPSHOT_FEATURE_SET, allow_file_id=False,
                                      pin_file_id=os.environ.get("SNAPSHOT_FILE_ID", "").strip())
    if not feats:                                                          # pinned/legacy model w/o recorded cols
        raise SystemExit("FATAL: champion manifest has no feature_cols — retrain with pm25-model@0.5.0.")
    missing = [c for c in feats if c not in sfeat]
    if missing:
        raise SystemExit(f"FATAL: snapshot missing champion feature cols {missing[:5]} — retrain the champion.")
    num = _num_np(np)
    n_h3 = snap.get("h3_id") or []; N = len(n_h3); si = list(range(N))
    cv = {c: snap.get(c) or [] for c in feats}
    Xte = np.array([[num(cv[c][i]) for c in feats] for i in si], dtype=float)
    r2 = ((man or {}).get("metrics") or {}).get("leave_station_out", {}).get("r2")
    conf = float(r2) if isinstance(r2, (int, float)) else 0.59
    print(f"INFER: loaded champion {(man or {}).get('id') or MODEL_FILE_ID} ({len(feats)} feats, r2={r2}) → "
          f"predict {N} cells (snapshot hour {smeta['observed_at']})", flush=True)
    preds = np.clip(model.predict(Xte), 0.0, CLIP_MAX)
    res_col = snap.get("resolution") or []; lon_col = snap.get("lon") or []; lat_col = snap.get("lat") or []
    obs = smeta["observed_at"] or NOW
    items = []
    for i in si:
        h = n_h3[i]
        if not h:
            continue
        lon = lon_col[i] if i < len(lon_col) else None; lat = lat_col[i] if i < len(lat_col) else None
        res = int(res_col[i]) if i < len(res_col) and res_col[i] is not None else 9
        oid = (f"ds:{TENANT}:air/quality/scenario/{SCENARIO}/{h}" if SCENARIO else f"ds:{TENANT}:air/quality/{h}")
        obj = {"schema": OUT_SCHEMA, "id": oid, "pm25": round(float(preds[i]), 1),
               "space": {"h3": h, "res": res, "centroid": [round(float(lon), 5), round(float(lat), 5)] if lon is not None and lat is not None else None},
               "method": "pm25-xgboost-fleet", "confidence": round(conf, 2), "observed_at": obs,
               "prov": {"method": "pm25-xgboost-fleet", "component": "ds:airawatdeveloper:engine/pm25-model@0.5.0",
                        "model": "xgboost", "model_ref": (man or {}).get("file_id"), "trained_on": (man or {}).get("trained_on"),
                        "features": len(feats), "validation": f"leave-station-out r2~{r2}", "confidence": round(conf, 2), "observed_at": obs}}
        obj["space"] = {k: v for k, v in obj["space"].items() if v is not None}
        items.append({"scope": OUT_SCHEMA, "id": oid, "object": obj, "valid_time": {"from": obs}})
    written, errs = 0, 0
    for k in range(0, len(items), 2000):
        chunk = items[k:k + 2000]
        r = call("/v1/write/batch", {"items": chunk, "asserted_at": NOW})
        written += r.get("written", 0); errs += r.get("error_count", 0)
        if r.get("errors"):
            print(f"  batch {k}: sample errors {r['errors'][:2]}", flush=True)
    pv = preds[preds > 0]
    print(f"INFER wrote {written} air.quality cells (errors {errs}) · pm25 range {round(float(pv.min()),1)}–{round(float(pv.max()),1)} "
          f"mean {round(float(pv.mean()),1)} µg/m³ @ {obs}", flush=True)
    print(f"RESULT: {json.dumps({'inference': True, 'loaded_champion': True, 'cells_written': written, 'errors': errs, 'features': len(feats), 'observed_at': obs, 'out_schema': OUT_SCHEMA, 'model_ref': (man or {}).get('file_id'), 'r2': r2})}")
    return 0


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — an engine runs in exactly one tenant.")
    if INFER:
        return infer_and_write()
    if PERSIST:                                                # WEEKLY: train + persist the champion air.model
        persist_model()
        return 0
    import numpy as np

    data, FEATURES, meta = _load_matrix()
    ycol = data.get(LABEL) or []
    N0 = len(ycol)
    # indices of labelled rows (all rows carry pm25_obs here, but guard anyway)
    lab_idx = [i for i in range(N0) if isinstance(ycol[i], (int, float))]
    if len(lab_idx) < 10:
        raise SystemExit(f"FATAL: only {len(lab_idx)} labelled rows — too few. No synthetic labels.")
    # SUBSAMPLE the INDEX LIST first (before building any array) — marshaling ~1M rows is the real cost on a
    # low-core box and blows the 1700s launcher cap. A random sample keeps every station (LSO valid) + season.
    MAX_ROWS = int(os.environ.get("MAX_ROWS", "100000"))
    rng0 = np.random.default_rng(0)
    if MAX_ROWS and len(lab_idx) > MAX_ROWS:
        idx = np.sort(rng0.choice(np.array(lab_idx), size=MAX_ROWS, replace=False))
    else:
        idx = np.array(lab_idx)
    print(f"tenant={TENANT} · matrix {meta['file_id'][:22]}… ({meta['format']}) · {N0} rows, "
          f"training on {len(idx)} labelled (of {len(lab_idx)}) · {len(FEATURES)} features", flush=True)

    def num(v):
        try:
            f = float(v); return f if f == f else np.nan
        except (TypeError, ValueError):
            return np.nan
    idxl = idx.tolist()
    y = np.array([num(ycol[i]) for i in idxl], dtype=float)
    fcolvals = {c: data.get(c) or [] for c in FEATURES}
    X = np.array([[num(fcolvals[c][i]) for c in FEATURES] for i in idxl], dtype=float)
    gcol = data.get("h3_id") or data.get("station") or [None] * N0
    ocol = data.get("observed_at") or [""] * N0
    groups = np.array([gcol[i] for i in idxl])
    years = np.array([str(ocol[i])[:4] for i in idxl])
    n, f = X.shape
    n_st = len(set(groups.tolist()))
    # pre-impute for the linear/tree-non-native models (column median; all-NaN col → 0). XGBoost gets raw NaN.
    col_med = np.nanmedian(X, axis=0); col_med = np.where(np.isnan(col_med), 0.0, col_med)
    Xi = np.where(np.isnan(X), col_med, X)

    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import Ridge
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.pipeline import make_pipeline
    from sklearn.model_selection import GroupKFold, cross_val_predict
    import xgboost as xgb

    def metrics(yt, yp):
        yt = np.asarray(yt, float); yp = np.asarray(yp, float); err = yp - yt
        rmse = float(np.sqrt(np.mean(err ** 2))); mae = float(np.mean(np.abs(err)))
        ss_res = float(np.sum(err ** 2)); ss_tot = float(np.sum((yt - yt.mean()) ** 2))
        return {"rmse": round(rmse, 2), "mae": round(mae, 2), "r2": round(1 - ss_res / ss_tot, 3) if ss_tot > 0 else None}

    jc = FEATURES.index("pm25_cams") if "pm25_cams" in FEATURES else None
    XP = dict(n_estimators=400, max_depth=6, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
              reg_lambda=1.0, tree_method="hist", random_state=0)
    def mk(kind):
        if kind == "ridge": return make_pipeline(StandardScaler(), Ridge(alpha=1.0)), Xi
        if kind == "rf":    return RandomForestRegressor(n_estimators=60, max_depth=18, n_jobs=-1, random_state=0), Xi
        return xgb.XGBRegressor(**XP), X

    # (A) LEAVE-STATION-OUT CV — GroupKFold by station: no station appears in both train & test (spatial holdout).
    folds = max(2, min(CV_FOLDS, n_st))
    gkf = GroupKFold(n_splits=folds)
    lso = {}
    if jc is not None:
        lso["baseline_cams"] = metrics(y, np.where(np.isnan(X[:, jc]), np.nanmean(X[:, jc]), X[:, jc]))
    lso["baseline_mean"] = metrics(y, np.full(n, y.mean()))
    for kind in ("ridge", "rf", "xgboost"):
        est, XX = mk(kind)
        yp = cross_val_predict(est, XX, y, cv=gkf, groups=groups, n_jobs=1)
        lso[{"ridge": "linear_ridge", "rf": "random_forest", "xgboost": "xgboost"}[kind]] = metrics(y, yp)
        print(f"  [LSO] {kind}: {lso[list(lso)[-1]]}", flush=True)

    # (B) TEMPORAL HOLDOUT — train < HOLDOUT_YEAR, test == HOLDOUT_YEAR (does it generalise forward in time?).
    tr = years < HOLDOUT_YEAR; te = years == HOLDOUT_YEAR
    tmp = {}
    if tr.sum() > 100 and te.sum() > 100:
        if jc is not None:
            tmp["baseline_cams"] = metrics(y[te], np.where(np.isnan(X[te, jc]), np.nanmean(X[:, jc]), X[te, jc]))
        tmp["baseline_mean"] = metrics(y[te], np.full(int(te.sum()), y[tr].mean()))
        for kind in ("ridge", "rf", "xgboost"):
            est, XX = mk(kind); est.fit(XX[tr], y[tr]); tmp[{"ridge": "linear_ridge", "rf": "random_forest", "xgboost": "xgboost"}[kind]] = metrics(y[te], est.predict(XX[te]))
        print(f"  [temporal {HOLDOUT_YEAR}] " + " ".join(f"{k}={v['rmse']}" for k, v in tmp.items()), flush=True)

    # SHAP (TreeSHAP) on a random sample — full 800k is too heavy. Fit on all, explain a sample.
    full = xgb.XGBRegressor(**XP); full.fit(X, y)
    rng = np.random.default_rng(0)
    samp = rng.choice(n, size=min(SHAP_SAMPLE, n), replace=False)
    contribs = full.get_booster().predict(xgb.DMatrix(X[samp], feature_names=FEATURES), pred_contribs=True)
    mean_abs = np.mean(np.abs(contribs[:, :-1]), axis=0)
    shap_top = [{"feature": k, "mean_abs_shap": round(v, 2)} for k, v in
                sorted(zip(FEATURES, mean_abs.tolist()), key=lambda t: -t[1])[:10]]
    print("  XGBoost top SHAP: " + ", ".join(f"{d['feature']}({d['mean_abs_shap']})" for d in shap_top), flush=True)

    out = {"model_family": "pm25", "tenant": TENANT, "feature_set": FEATURE_SET, "matrix_file_id": meta["file_id"],
           "n_rows": n, "n_stations": n_st, "n_features": f, "cv_folds": folds,
           "validation": "leave-station-out (GroupKFold) + temporal-holdout",
           "leave_station_out": lso, "temporal_holdout": {"year": HOLDOUT_YEAR, "n_train": int(tr.sum()), "n_test": int(te.sum()), "metrics": tmp},
           "shap_top": shap_top, "pm25_obs_range": [round(float(y.min()), 1), round(float(y.max()), 1)],
           "prov": {"component": "ds:airawatdeveloper:engine/pm25-model@0.3.0", "built_at": NOW}}
    print(f"RESULT: {json.dumps(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
