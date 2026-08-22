#!/usr/bin/env python3
"""buildings-openbuildings — BUILDINGS from Google **Open Buildings v3** via GEE (ADR-0060 L0→L2, Wave-1).

Open Buildings (`GOOGLE/Research/open-buildings/v3/polygons`) is an ML-derived footprint layer — far more
complete than OSM in Indian cities (incl. informal settlements). We read it SERVER-SIDE via GEE (the shared
EO key, same runtime as land cover / terrain / hydrology) — no per-S2 CSV downloads, no broker size caps.

Per H3 cell we compute the **building footprint coverage** (fraction of the cell under a building, from the
rasterised footprint mask at ~4 m) and the derived **built area (m²)**. Writes `cell.buildings` (the L2
analytical layer City Scan renders). Native footprints (`geo.building`) are a follow-up (100k+ polygons).

TRUST: real Open Buildings only (confidence ≥ CONF). NO synthetic fallback; fails loudly if GEE returns
nothing. RESUMABLE — skips cells already written. No tenant/AOI default — a driver never guesses its tenant.

  Env: TENANT · AOI_ID (opt; else discover) · GEE_SERVICE_ACCOUNT_KEY (Vault) · GEE_PROJECT/GEE_SERVICE_ACCOUNT
       BATCH (cells/reduceRegions, default 150) · RESUME (1) · CONF (min footprint confidence, default 0.70)
"""
import json, math, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_HINT = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""   # NO default — never guess the tenant
AOI_REF = ""
PROJECT = os.environ.get("GEE_PROJECT", "")
SA_EMAIL = os.environ.get("GEE_SERVICE_ACCOUNT", "")
SA_KEY = os.environ.get("GEE_SERVICE_ACCOUNT_KEY", "")
BATCH = int(os.environ.get("BATCH", "150"))
CONF = float(os.environ.get("CONF", "0.70"))
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/google-open-buildings"
DERIVED = ["gee:GOOGLE/Research/open-buildings/v3/polygons"]
OB_ASSET = "GOOGLE/Research/open-buildings/v3/polygons"


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def unwrap(res):
    return res.get("object") if isinstance(res, dict) and "object" in res else res


def _load_aoi(aoi_id):
    o = unwrap(call("/v1/read", {"scope": "space.aoi", "id": aoi_id}))
    if not isinstance(o, dict) or len(o.get("bbox") or []) != 4:
        raise SystemExit(f"FATAL: space.aoi {aoi_id} missing / no 4-el bbox: {o!r}")
    return o, [float(v) for v in o["bbox"]], int(o.get("h3_res", 9))


def resolve_aoi():
    if AOI_HINT:
        ht = AOI_HINT.split(":")[1] if AOI_HINT.count(":") >= 2 else ""
        if ht and ht != TENANT:
            raise SystemExit(f"FATAL: AOI_ID {AOI_HINT} belongs to tenant '{ht}', not write tenant '{TENANT}'.")
        o, bbox, res = _load_aoi(AOI_HINT)
        return o, bbox, res, AOI_HINT
    rows = call("/v1/query", {"scope": "space.aoi"}).get("results") or []
    mine = [(r.get("object") or r) for r in rows
            if str((r.get("object") or r).get("id", "")).startswith(f"ds:{TENANT}:")
            and len((r.get("object") or r).get("bbox") or []) == 4]
    if not mine:
        raise SystemExit(f"FATAL: no space.aoi for tenant '{TENANT}' — run City Setup first. No default AOI.")
    o = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))
    return o, [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), o["id"]


def init_ee():
    try:
        import ee
    except ImportError:
        raise SystemExit("FATAL: earthengine-api not installed — this driver runs in the earth-observation runtime.")
    if not SA_KEY:
        raise SystemExit("FATAL: GEE_SERVICE_ACCOUNT_KEY not set — load it into Vault (Connections). No synthetic fallback.")
    key_data = open(SA_KEY).read() if (SA_KEY.endswith(".json") and os.path.exists(SA_KEY)) else SA_KEY
    kj = {}
    try:
        kj = json.loads(key_data)
    except Exception:
        pass
    email = SA_EMAIL or kj.get("client_email")
    project = PROJECT or kj.get("project_id")
    if not (email and project):
        raise SystemExit("FATAL: could not resolve GEE service-account email / project from the key.")
    creds = ee.ServiceAccountCredentials(email, key_data=key_data)
    ee.Initialize(creds, project=project, opt_url="https://earthengine-highvolume.googleapis.com")
    print(f"  authenticated GEE as {email} · project {project}", flush=True)
    return ee


def h3_cells(bbox, res):
    import h3
    w, s, e, n = bbox
    return list(h3.polygon_to_cells(h3.LatLngPoly([(s, w), (s, e), (n, e), (n, w)]), res))


def cell_boundary_lonlat(hcell):
    import h3
    return [[lo, la] for (la, lo) in h3.cell_to_boundary(hcell)]


def main():
    global AOI_REF
    import h3
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant. Refusing to guess.")
    aoi, bbox, res, AOI_REF = resolve_aoi()
    ee = init_ee()
    cells = h3_cells(bbox, res)
    print(f"Open Buildings v3 · AOI {AOI_REF} bbox={bbox} res={res} → {len(cells)} H3 cells (conf≥{CONF})", flush=True)

    if os.environ.get("RESUME", "1") != "0":
        done = set()
        try:
            for r in (call("/v1/query", {"scope": "cell.buildings"}).get("results") or []):
                hid = ((r.get("object") or r).get("cell_ref") or {}).get("h3_id")
                if hid:
                    done.add(hid)
        except Exception:
            pass
        if done:
            before = len(cells); cells = [c for c in cells if c not in done]
            print(f"  resuming: {len(done)} done → {len(cells)}/{before} remaining", flush=True)
    if not cells:
        print("  nothing to do — all cells already indexed"); return 0

    rect = ee.Geometry.Rectangle(bbox)
    fc = ee.FeatureCollection(OB_ASSET).filterBounds(rect).filter(ee.Filter.gte("confidence", CONF))
    built = fc.reduceToImage(["area_in_meters"], ee.Reducer.first()).gt(0).unmask(0).rename("built")   # 1 where a footprint covers the pixel

    written = failed = 0
    nb = (len(cells) + BATCH - 1) // BATCH
    for i in range(0, len(cells), BATCH):
        chunk = cells[i:i + BATCH]
        fcoll = ee.FeatureCollection([ee.Feature(ee.Geometry.Polygon([cell_boundary_lonlat(hc)]), {"h3": hc}) for hc in chunk])
        red = built.reduceRegions(collection=fcoll, reducer=ee.Reducer.mean(), scale=4)   # mean of the mask = footprint coverage
        rows = None
        for attempt in range(5):
            try:
                rows = red.getInfo().get("features") or []; break
            except Exception as ex:
                if attempt == 4:
                    print(f"  ⚠ batch {i//BATCH+1}/{nb} failed ({type(ex).__name__}: {str(ex)[:60]}) — skip", flush=True)
                else:
                    time.sleep(3 * (attempt + 1))
        if rows is None:
            failed += 1; continue
        for f in rows:
            p = f.get("properties") or {}
            hc = p.get("h3"); cov = p.get("mean")
            if hc is None or not isinstance(cov, (int, float)):
                continue                                          # no OB coverage computed for this cell → skip, don't fabricate
            la, lo = h3.cell_to_latlng(hc)
            if not (bbox[0] <= lo <= bbox[2] and bbox[1] <= la <= bbox[3]):
                continue                                          # AOI membership guard
            area_km2 = h3.cell_area(hc, unit="km^2")
            fact = {
                "schema": "cell.buildings", "id": f"ds:{TENANT}:cell/buildings/{hc}",
                "cell_ref": {"h3_id": hc, "resolution": res, "centroid": [round(lo, 6), round(la, 6)],
                             "boundary": [[round(x, 6), round(y, 6)] for (x, y) in cell_boundary_lonlat(hc)]},
                "building_coverage": round(max(0.0, min(1.0, float(cov))), 4),
                "built_area_m2": round(max(0.0, min(1.0, float(cov))) * area_km2 * 1e6, 1),
                "method": "open-buildings-v3", "source_ref": SRC, "layer": "L2", "confidence": 0.85,
                "derived_from": DERIVED,
                "prov": {"method": "open-buildings-v3", "confidence": 0.85, "source_ref": SRC,
                         "derived_from": DERIVED, "min_footprint_confidence": CONF, "observed_at": NOW},
                "observed_at": NOW}
            call("/v1/write", {"scope": "cell.buildings", "id": fact["id"], "object": fact,
                               "valid_time": {"from": NOW}, "asserted_at": NOW})
            written += 1
        print(f"  …{min(i + BATCH, len(cells))}/{len(cells)} cells (wrote {written})", flush=True)

    if not written:
        raise SystemExit("FATAL: Open Buildings returned no cell coverage — nothing written (no synthetic fill).")
    if failed:
        print(f"  ⚠ {failed}/{nb} batches skipped — wrote {written} REAL cells (partial, no fill)", flush=True)
    print(f"wrote {written} cell.buildings cells from Google Open Buildings v3 (real GEE)")
    print(f"RESULT: {json.dumps({'source': 'open-buildings-v3', 'cells': written, 'batches_failed': failed})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
