#!/usr/bin/env python3
"""PM2.5 SOURCE APPORTIONMENT — Slice 1 (SHAP-proxy). ADR-0073 follow-on.

Answers "what is CAUSING the PM2.5 here?" per H3 cell, per hour, by reusing the deployed nowcast:
  1. load the CHAMPION air.model (the same XGBoost booster pm25-model INFER loads) + its feature_cols,
  2. load the latest air.feature_matrix snapshot (the same (h3 × latest-hour) grid the nowcast scores),
  3. compute EXACT per-cell TreeSHAP contributions with XGBoost `pred_contribs=True` (native — no shap lib),
  4. GROUP each feature's contribution into a SOURCE category via SOURCE_MAP, keeping METEOROLOGY and
     TERRAIN as MODULATORS (they disperse/retain pollution, they do not emit it — never counted as a source),
  5. write air.source_apportionment (per-cell % split + dominant source) and an air.source_mix city rollup.

HONEST LIMITATIONS (stamped into every record as method=`shap-xgb-proxy`):
  • This is ASSOCIATION via the model's inputs, not measured emission mass. A rigorous mass apportionment
    needs chemical speciation (a receptor/PMF model) or a source-tagged chemical-transport model — Slice 2.
  • Biomass / stubble burning is only weakly covered: the feature matrix has no direct fire column yet
    (cell.fire is not joined by pm25-features), so burning shows up only indirectly via AOD/regional terms.
No synthetic fill — a cell with no positive source attribution is written with low confidence, not guessed.
"""
import base64, json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
FEATURE_SET = os.environ.get("FEATURE_SET", "pm25-train-cpcb").strip()          # which champion to load
SNAPSHOT_FEATURE_SET = os.environ.get("SNAPSHOT_FEATURE_SET", "pm25").strip()    # the (h3 × latest-hour) grid
SNAPSHOT_FILE_ID = os.environ.get("SNAPSHOT_FILE_ID", "").strip()
MODEL_FILE_ID = os.environ.get("MODEL_FILE_ID", "").strip()
MODEL_SCHEMA = "air.model"
OUT_SCHEMA = "air.source_apportionment"
MIX_SCHEMA = "air.source_mix"
MIN_SHARE = float(os.environ.get("MIN_SHARE", "0.0"))     # drop source shares below this from the per-cell split
WRITE_MIX = os.environ.get("WRITE_MIX", "1").strip() in ("1", "true", "yes")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

# ── feature → source-category map ─────────────────────────────────────────────────────────────────────────
# SOURCE categories are counted in the % split; MODULATORS are reported separately (signed µg), never as a
# source. Lag/neighbour variants inherit their base feature's category. Kept in-engine for Slice 1; the
# intended home is a governed ref.domain vocab + a governance.policy_rule map so an air steward can edit it
# without a code change (Slice 1.1).
SOURCE_MAP = {
    # regional / transboundary background (the CAMS regional model + aerosol column + spatial/temporal context)
    "pm25_cams": "regional_background", "pm10_cams": "dust", "aod": "regional_background",
    "aod_valid_fraction": "regional_background",
    # traffic / on-road combustion
    "no2_cams": "traffic", "road_length_m": "traffic", "road_density": "traffic",
    # industry / power (SO2 + night-time economic activity)
    "so2_cams": "industry", "nightlight_radiance": "industry",
    # secondary aerosol (photochemistry)
    "o3_cams": "secondary",
    # construction / resuspended urban dust (built fabric)
    "built_area_m2": "construction_dust", "building_coverage": "construction_dust",
    "building_mean_height_m": "construction_dust", "built_volume_m3": "construction_dust",
    "builtup_frac": "construction_dust",
    # residential (domestic cooking/heating ~ population density)
    "population": "residential",
    # MODULATORS — disperse / retain, not emit (reported separately, excluded from the source split)
    "pblh_m": "meteorology", "rainfall_mm": "meteorology",
    "elevation_m": "geographic_context", "slope_deg": "geographic_context",
    "flood_susceptibility": "geographic_context", "hand_mean_m": "geographic_context",
    "greencover_frac": "geographic_context",
}
MODULATOR_CATS = {"meteorology", "geographic_context"}
# every source category we report a share for (so a dominant can be chosen even when a category is 0)
SOURCE_CATS = ["traffic", "industry", "construction_dust", "dust", "residential", "secondary",
               "biomass_burning", "regional_background"]


def _cat(feat):
    base = feat[:-4] if feat.endswith("_nbr") else feat          # H3-neighbour variant → base category
    for lag in ("_lag1", "_lag3", "_lag6", "_lag24"):            # temporal lag variant → base category
        if base.endswith(lag):
            base = base[: -len(lag)]
    return SOURCE_MAP.get(base, SOURCE_MAP.get(feat, "regional_background"))


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def _rows(scope):
    return [(r.get("object") or r) for r in (call("/v1/query", {"scope": scope}).get("results") or [])]


def _num(np):
    def f(v):
        try:
            return float(v) if v is not None and v != "" else np.nan
        except Exception:
            return np.nan
    return f


def _load_matrix():
    """Resolve the latest air.feature_matrix snapshot manifest → fetch its blob → (data dict, feature_cols)."""
    import io
    mans = [m for m in _rows("air.feature_matrix") if m.get("file_id")]
    if SNAPSHOT_FILE_ID:
        mans = [m for m in mans if m.get("file_id") == SNAPSHOT_FILE_ID]
    else:
        mans = [m for m in mans if m.get("feature_set") == SNAPSHOT_FEATURE_SET]
    if not mans:
        raise SystemExit(f"FATAL: no air.feature_matrix (feature_set={SNAPSHOT_FEATURE_SET}) — run pm25-features first.")
    man = max(mans, key=lambda m: m.get("observed_at") or "")
    fcols, obs, fmt = man.get("feature_cols"), man.get("observed_at"), man.get("format", "parquet")
    raw = base64.b64decode(call("/v1/file/get", {"id": man["file_id"]}).get("content_b64") or "")
    if fmt == "parquet":
        import pyarrow.parquet as pq
        tbl = pq.read_table(io.BytesIO(raw)); data = {c: tbl.column(c).to_pylist() for c in tbl.column_names}
    else:
        rows = [json.loads(l) for l in raw.decode().splitlines() if l.strip()]
        cols = man.get("columns") or (list(rows[0].keys()) if rows else [])
        data = {c: [r.get(c) for r in rows] for c in cols}
    return data, (fcols or [c for c in data if c not in ("h3_id", "resolution", "lon", "lat", "observed_at", "pm25_obs")]), obs


def _load_champion():
    """Resolve the CHAMPION air.model (or a pinned MODEL_FILE_ID) → (xgb model, feature_cols, manifest)."""
    import xgboost as xgb, tempfile
    if MODEL_FILE_ID:
        hit = [m for m in _rows(MODEL_SCHEMA) if m.get("file_id") == MODEL_FILE_ID]
        man = hit[0] if hit else {"file_id": MODEL_FILE_ID, "feature_cols": None}
    else:
        cid = f"ds:{TENANT}:air/model/{FEATURE_SET}"
        hit = [m for m in _rows(MODEL_SCHEMA) if m.get("id") == cid and m.get("champion") and m.get("file_id")]
        if not hit:
            hit = [m for m in _rows(MODEL_SCHEMA) if m.get("feature_set") == FEATURE_SET and m.get("champion") and m.get("file_id")]
        if not hit:
            raise SystemExit(f"FATAL: no champion air.model (feature_set={FEATURE_SET}) — train pm25-model first.")
        man = max(hit, key=lambda m: m.get("observed_at") or "")
    raw = base64.b64decode(call("/v1/file/get", {"id": man["file_id"]}).get("content_b64") or "")
    tmp = tempfile.mktemp(suffix=".json"); open(tmp, "wb").write(raw)
    model = xgb.XGBRegressor(); model.load_model(tmp)
    return model, man.get("feature_cols"), man


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — an engine runs in exactly one tenant.")
    import numpy as np, xgboost as xgb
    model, feats, man = _load_champion()
    snap, sfeat, obs = _load_matrix()
    obs = obs or NOW
    if not feats:
        raise SystemExit("FATAL: champion manifest has no feature_cols — retrain pm25-model.")
    missing = [c for c in feats if c not in sfeat]
    if missing:
        raise SystemExit(f"FATAL: snapshot missing champion feature cols {missing[:5]} — rebuild features / retrain.")
    num = _num(np)
    h3 = snap.get("h3_id") or []
    N = len(h3)
    cv = {c: snap.get(c) or [] for c in feats}
    X = np.array([[num(cv[c][i]) for c in feats] for i in range(N)], dtype=float)
    r2 = ((man or {}).get("metrics") or {}).get("leave_station_out", {}).get("r2")
    conf = float(r2) if isinstance(r2, (int, float)) else 0.59

    # EXACT per-cell TreeSHAP via XGBoost native pred_contribs (shape (N, F+1); last column = bias/base value)
    booster = model.get_booster()
    dm = xgb.DMatrix(X, feature_names=list(feats))
    contribs = booster.predict(dm, pred_contribs=True)
    bias = contribs[:, -1]
    feat_contribs = contribs[:, :-1]
    pred = contribs.sum(axis=1)                               # == model.predict(X)
    cat_of = [_cat(f) for f in feats]
    res_col = snap.get("resolution") or []; lon_col = snap.get("lon") or []; lat_col = snap.get("lat") or []

    items = []
    mix_acc = {c: 0.0 for c in SOURCE_CATS}; mix_n = 0; dom_count = {}
    for i in range(N):
        h = h3[i]
        if not h:
            continue
        src = {c: 0.0 for c in SOURCE_CATS}
        mod = {"meteorology": 0.0, "geographic_context": 0.0}
        for j, c in enumerate(cat_of):
            v = float(feat_contribs[i, j])
            if c in MODULATOR_CATS:
                mod[c] += v
            else:
                src[c] = src.get(c, 0.0) + v
        src["regional_background"] += float(bias[i])          # base value = ambient/regional baseline
        pos = {c: (v if v > 0 else 0.0) for c, v in src.items()}
        tot = sum(pos.values())
        if tot > 0:
            frac = {c: round(pos[c] / tot, 4) for c in SOURCE_CATS}
            cell_conf = round(conf, 2)
        else:                                                  # no positive source attribution → explicit low-confidence
            frac = {c: 0.0 for c in SOURCE_CATS}; cell_conf = 0.0
        if MIN_SHARE > 0:
            frac = {c: v for c, v in frac.items() if v >= MIN_SHARE}
        dominant = max(frac, key=frac.get) if frac and tot > 0 else None
        lon = lon_col[i] if i < len(lon_col) else None; lat = lat_col[i] if i < len(lat_col) else None
        res = int(res_col[i]) if i < len(res_col) and res_col[i] is not None else 9
        oid = f"ds:{TENANT}:air/source_apportionment/{h}"
        obj = {"schema": OUT_SCHEMA, "id": oid, "observed_at": obs,
               "pm25": round(float(pred[i]), 1), "dominant_source": dominant,
               "fractions": frac,
               "modulators_ugm3": {k: round(v, 2) for k, v in mod.items()},   # signed; negative = dispersion helping
               "space": {"h3": h, "res": res,
                         "centroid": [round(float(lon), 5), round(float(lat), 5)] if lon is not None and lat is not None else None},
               "method": "shap-xgb-proxy", "confidence": cell_conf,
               "prov": {"method": "shap-xgb-proxy", "component": "ds:airawatdeveloper:engine/pm25-apportion@0.1.0",
                        "model_ref": (man or {}).get("file_id"), "feature_set": FEATURE_SET,
                        "note": "association via model inputs, not measured source mass; biomass weakly covered (no cell.fire feature)",
                        "observed_at": obs}}
        obj["space"] = {k: v for k, v in obj["space"].items() if v is not None}
        items.append({"scope": OUT_SCHEMA, "id": oid, "object": obj, "valid_time": {"from": obs}})
        if tot > 0:
            mix_n += 1
            for c in SOURCE_CATS:
                mix_acc[c] += frac[c]
            if dominant:
                dom_count[dominant] = dom_count.get(dominant, 0) + 1

    written, errs = 0, 0
    for k in range(0, len(items), 2000):
        r = call("/v1/write/batch", {"items": items[k:k + 2000], "asserted_at": NOW})
        written += r.get("written", 0); errs += r.get("error_count", 0)
        if r.get("errors"):
            print(f"  batch {k}: sample errors {r['errors'][:2]}", flush=True)

    mix_obj = None
    if WRITE_MIX and mix_n:
        avg = {c: round(mix_acc[c] / mix_n, 4) for c in SOURCE_CATS}
        mix_dom = max(avg, key=avg.get)
        mid = f"ds:{TENANT}:air/source_mix/latest"
        mix_obj = {"schema": MIX_SCHEMA, "id": mid, "observed_at": obs, "n_cells": mix_n,
                   "fractions": avg, "dominant_source": mix_dom,
                   "dominant_cell_counts": dom_count, "method": "shap-xgb-proxy", "confidence": round(conf, 2),
                   "prov": {"component": "ds:airawatdeveloper:engine/pm25-apportion@0.1.0", "model_ref": (man or {}).get("file_id"),
                            "feature_set": FEATURE_SET, "observed_at": obs}}
        call("/v1/write", {"scope": MIX_SCHEMA, "id": mid, "object": mix_obj, "valid_time": {"from": obs}, "asserted_at": NOW})

    print(f"APPORTION wrote {written} air.source_apportionment cells (errors {errs}) @ {obs}", flush=True)
    if mix_obj:
        print(f"  city source mix: {json.dumps(mix_obj['fractions'])} · dominant={mix_obj['dominant_source']} over {mix_obj['n_cells']} cells", flush=True)
    print(f"RESULT: {json.dumps({'apportioned': True, 'cells_written': written, 'errors': errs, 'observed_at': obs, 'out_schema': OUT_SCHEMA, 'method': 'shap-xgb-proxy', 'model_ref': (man or {}).get('file_id'), 'city_mix': (mix_obj or {}).get('fractions')})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
