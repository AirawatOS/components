#!/usr/bin/env python3
"""population-ghsl — resident POPULATION per H3 cell from **GHS-POP** (JRC GHSL) via GEE (ADR-0060 L2, Wave-1).

GHS-POP (`JRC/GHSL/P2023A/GHS_POP`) is the JRC Global Human Settlement population grid (100 m). We read it
SERVER-SIDE via GEE (the shared EO key, same runtime as land cover / buildings) and SUM the population raster
inside each H3 cell → `demographics.population` (count + density_per_km2), the L2 measure that is the denominator
for accessibility / livability. Population is the governed noun `demographics.population` (a cell measure whose
cell-ness lives in the `space` block, ADR-0053) — NOT a new `cell.population`.

TRUST: real GHSL only. NO synthetic fallback; fails loudly if GEE returns nothing. RESUMABLE — skips cells already
written. No tenant/AOI default — a driver never guesses its tenant.

  Env: TENANT · AOI_ID (opt; else discover) · GEE_SERVICE_ACCOUNT_KEY (Vault) · GEE_PROJECT/GEE_SERVICE_ACCOUNT
       EPOCH (GHSL epoch year, default 2020 — observed; 1975..2030 in 5-yr steps) · BATCH (cells/reduceRegions, 150)
       RESUME (1)
"""
import json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_HINT = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""   # NO default — never guess the tenant
PROJECT = os.environ.get("GEE_PROJECT", "")
SA_EMAIL = os.environ.get("GEE_SERVICE_ACCOUNT", "")
SA_KEY = os.environ.get("GEE_SERVICE_ACCOUNT_KEY", "")
BATCH = int(os.environ.get("BATCH", "150"))
EPOCH = int(os.environ.get("EPOCH", "2020"))
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
OBSERVED = f"{EPOCH}-01-01T00:00:00Z"
SRC = "ds:airawat:ref/source/ghsl"
POP_ASSET = "JRC/GHSL/P2023A/GHS_POP"
DERIVED = [f"gee:{POP_ASSET}:E{EPOCH}"]


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


def pop_image(ee):
    """The GHS-POP population_count image for the requested epoch (the collection carries one image per epoch)."""
    coll = ee.ImageCollection(POP_ASSET)
    img = coll.filterDate(f"{EPOCH}-01-01", f"{EPOCH + 1}-01-01").first()
    img = ee.Image(ee.Algorithms.If(img, img, coll.filterDate(f"{EPOCH - 2}-01-01", f"{EPOCH + 3}-01-01").mosaic()))
    return img.select([0]).rename("pop").unmask(0)


def h3_cells(bbox, res):
    import h3
    w, s, e, n = bbox
    return list(h3.polygon_to_cells(h3.LatLngPoly([(s, w), (s, e), (n, e), (n, w)]), res))


def cell_boundary_lonlat(hcell):
    import h3
    return [[lo, la] for (la, lo) in h3.cell_to_boundary(hcell)]


def main():
    import h3
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant. Refusing to guess.")
    aoi, bbox, res, aoi_ref = resolve_aoi()
    ee = init_ee()
    cells = h3_cells(bbox, res)
    print(f"GHS-POP E{EPOCH} · AOI {aoi_ref} bbox={bbox} res={res} → {len(cells)} H3 cells", flush=True)

    if os.environ.get("RESUME", "1") != "0":
        done = set()
        try:
            for r in (call("/v1/query", {"scope": "demographics.population"}).get("results") or []):
                sp = ((r.get("object") or r).get("space") or {})
                if sp.get("h3"):
                    done.add(sp["h3"])
        except Exception:
            pass
        if done:
            before = len(cells); cells = [c for c in cells if c not in done]
            print(f"  resuming: {len(done)} done → {len(cells)}/{before} remaining", flush=True)
    if not cells:
        print("  nothing to do — all cells already have population"); return 0

    img = pop_image(ee)
    written = failed = 0
    nb = (len(cells) + BATCH - 1) // BATCH
    for i in range(0, len(cells), BATCH):
        chunk = cells[i:i + BATCH]
        fcoll = ee.FeatureCollection([ee.Feature(ee.Geometry.Polygon([cell_boundary_lonlat(hc)]), {"h3": hc}) for hc in chunk])
        red = img.reduceRegions(collection=fcoll, reducer=ee.Reducer.sum(), scale=100)   # sum people in each H3 cell
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
            hc = p.get("h3"); pop = p.get("sum")
            if hc is None or not isinstance(pop, (int, float)):
                continue                                          # no GHSL value computed → skip, don't fabricate
            la, lo = h3.cell_to_latlng(hc)
            if not (bbox[0] <= lo <= bbox[2] and bbox[1] <= la <= bbox[3]):
                continue                                          # AOI membership guard
            pop = max(0.0, float(pop))
            area_km2 = h3.cell_area(hc, unit="km^2")
            fact = {
                "schema": "demographics.population", "id": f"ds:{TENANT}:demographics/population/{hc}",
                "space": {"h3": hc, "res": res, "centroid": [round(lo, 6), round(la, 6)],
                          "boundary": [[round(x, 6), round(y, 6)] for (x, y) in cell_boundary_lonlat(hc)]},
                "population": round(pop, 1),
                "density_per_km2": round(pop / area_km2, 1) if area_km2 else None,
                "epoch": EPOCH, "source": "GHSL R2023A (GHS-POP)", "source_ref": SRC,
                "method": "ghsl-p2023a-reduceRegions-sum", "layer": "L2", "confidence": 0.9,
                "derived_from": DERIVED,
                "prov": {"method": "ghsl-p2023a", "source_ref": SRC, "derived_from": DERIVED, "epoch": EPOCH,
                         "confidence": 0.9, "observed_at": OBSERVED},
                "observed_at": OBSERVED}
            call("/v1/write", {"scope": "demographics.population", "id": fact["id"], "object": fact,
                               "valid_time": {"from": OBSERVED}, "asserted_at": NOW})
            written += 1
        print(f"  …{min(i + BATCH, len(cells))}/{len(cells)} cells (wrote {written})", flush=True)

    if not written:
        raise SystemExit("FATAL: GHS-POP returned no population for any cell — nothing written (no synthetic fill).")
    if failed:
        print(f"  ⚠ {failed}/{nb} batches skipped — wrote {written} REAL cells (partial, no fill)", flush=True)
    print(f"wrote {written} demographics.population cells from GHS-POP E{EPOCH} (real GEE)")
    print(f"RESULT: {json.dumps({'source': 'ghsl-p2023a', 'epoch': EPOCH, 'cells': written, 'batches_failed': failed})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
