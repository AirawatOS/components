#!/usr/bin/env python3
"""change-detect-alphaearth — flag parcels where the built form CHANGED between two years, from Google's AlphaEarth
Foundations satellite embeddings (GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL — a 64-D per-pixel vector that encodes surface
state). For each geo.parcel we take the MEAN embedding in year A and year B; AlphaEarth vectors are unit-normalised, so
the cosine DISTANCE (1 − A·B) is a physically-meaningful change magnitude. Above a threshold we write a tax.change_signal
(new_build / expanded), which the tax-gap-engine uses as corroborating evidence that a parcel is unassessed/under-assessed.

REAL data only — this runs on Earth Engine. If GEE is unauthenticated / earthengine-api is absent it FAILS LOUDLY; it
NEVER fabricates a change. Credentials: a Google service-account key (GEE_SERVICE_ACCOUNT_KEY, Vault-materialised).
"""
import os, json, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
PROJECT = os.environ.get("GEE_PROJECT", "")
SA_EMAIL = os.environ.get("GEE_SERVICE_ACCOUNT", "")
SA_KEY = os.environ.get("GEE_SERVICE_ACCOUNT_KEY", "")
FROM_YEAR = os.environ.get("FROM_YEAR", "2022")
TO_YEAR = os.environ.get("TO_YEAR", "2024")
CHANGE_MIN = float(os.environ.get("CHANGE_MIN", "0.35"))       # cosine-distance threshold to raise a signal
NEW_BUILD_MIN = float(os.environ.get("NEW_BUILD_MIN", "0.6"))  # above this = likely NEW construction (vs expansion)
BATCH = int(os.environ.get("BATCH", "200"))
DATASET = os.environ.get("ALPHAEARTH", "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read() or b"{}")


def gee():
    try:
        import ee
    except ImportError:
        raise SystemExit("FATAL: earthengine-api not installed (pip install earthengine-api) — no synthetic fallback")
    if not SA_KEY:
        raise SystemExit("FATAL: GEE_SERVICE_ACCOUNT_KEY not set — load the service-account key into Vault. No fallback.")
    key_data = open(SA_KEY).read() if (SA_KEY.endswith(".json") and os.path.exists(SA_KEY)) else SA_KEY
    kj = {}
    try: kj = json.loads(key_data)
    except Exception: pass
    email, project = SA_EMAIL or kj.get("client_email"), PROJECT or kj.get("project_id")
    if not (email and project):
        raise SystemExit("FATAL: could not resolve GEE service-account email / project — provide a full SA key JSON.")
    creds = ee.ServiceAccountCredentials(email, key_data=key_data)
    ee.Initialize(creds, project=project, opt_url="https://earthengine-highvolume.googleapis.com")
    print(f"  authenticated GEE as {email} · project {project}", flush=True)
    return ee


def _rings(geom):
    if not isinstance(geom, dict):
        return None
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "Polygon" and c:
        return c
    if t == "MultiPolygon" and c and c[0]:
        return c[0]
    return None


def _year_embedding(ee, year):
    return ee.ImageCollection(DATASET).filterDate(f"{year}-01-01", f"{year}-12-31").mosaic()


def main():
    if not TENANT:
        print("[change-detect] no tenant in this context — nothing to do"); return 0
    parcels = [r.get("object") or r for r in call("/v1/query", {"scope": "geo.parcel", "limit": 20000}).get("results", [])]
    parcels = [p for p in parcels if _rings(p.get("geometry"))]
    if not parcels:
        print("[change-detect] no parcels with geometry — load the cadastre first (geo.parcel). Nothing to detect."); return 0
    ee = gee()
    imgA, imgB = _year_embedding(ee, FROM_YEAR), _year_embedding(ee, TO_YEAR)
    bands = imgA.bandNames()
    written = 0
    print(f"[change-detect] {len(parcels)} parcels · AlphaEarth {FROM_YEAR}→{TO_YEAR} · threshold {CHANGE_MIN}", flush=True)
    for i in range(0, len(parcels), BATCH):
        chunk = parcels[i:i + BATCH]
        feats = [ee.Feature(ee.Geometry.Polygon(_rings(p["geometry"])), {"pid": p["parcel_id"]}) for p in chunk]
        fc = ee.FeatureCollection(feats)
        # per-parcel MEAN 64-vector for each year, in one server-side reduce each
        mA = imgA.reduceRegions(collection=fc, reducer=ee.Reducer.mean(), scale=10).getInfo()["features"]
        mB = imgB.reduceRegions(collection=fc, reducer=ee.Reducer.mean(), scale=10).getInfo()["features"]
        bn = bands.getInfo()
        bB = {f["properties"]["pid"]: f["properties"] for f in mB}
        for fa in mA:
            pid = fa["properties"]["pid"]; pa, pb = fa["properties"], bB.get(pid)
            if not pb:
                continue
            va = [pa.get(b) for b in bn]; vb = [pb.get(b) for b in bn]
            if any(x is None for x in va) or any(x is None for x in vb):
                continue                                        # no coverage this parcel/year — skip, don't invent
            dot = sum(a * b for a, b in zip(va, vb))            # AlphaEarth vectors are ~unit-norm → dot ≈ cosine sim
            dist = max(0.0, min(1.0, round((1 - dot) / 2.0 * 2, 4)))   # cosine distance, clamped to [0,1]
            if dist < CHANGE_MIN:
                continue
            ctype = "new_build" if dist >= NEW_BUILD_MIN else "expanded"
            sid = f"ds:{TENANT}:tax/change_signal/{pid.rsplit('/', 1)[-1]}-{FROM_YEAR}_{TO_YEAR}"
            call("/v1/write", {"scope": "tax.change_signal", "id": sid, "object": {
                "schema": "tax.change_signal", "id": sid, "tenant": TENANT, "parcel_id": pid,
                "change_type": ctype, "score": dist, "from_epoch": FROM_YEAR, "to_epoch": TO_YEAR,
                "observed_at": NOW, "source": f"alphaearth:{DATASET}", "created_at": NOW}})
            written += 1
    print(f"[change-detect] wrote {written} change signals (dist ≥ {CHANGE_MIN})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
