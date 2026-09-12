#!/usr/bin/env python3
"""maiac-aod — SATELLITE AOD from MODIS MAIAC (Google Earth Engine) → cell.aod (ADR-0073 M2).

MAIAC (`MODIS/061/MCD19A2_GRANULES`, 1 km, Terra+Aqua) is the best free high-resolution aerosol product. Its
`Optical_Depth_055` band is column aerosol optical depth at 550 nm — the single most useful SATELLITE predictor
of surface PM2.5. It is a PROXY, not a surface measurement: the model learns the AOD→surface relationship gated
by boundary-layer height + humidity (ADR-0073 §2). This driver reduces the 1 km AOD server-side per H3 cell over
a short recent window → mean + std + the VALID (cloud-free) fraction. Missing satellite data is EXPECTED and
represented explicitly (valid_fraction, per-cell) — never gap-filled or upsampled.

Its ONLY I/O is the kernel broker:
  read   the tenant's space.aoi frame (discovered — never a hardcoded default)
  fetch  Google Earth Engine (MAIAC), egress-gated
  write  cell.aod — aod, aod_std, valid_fraction per H3 cell for the recent window

REAL MAIAC only — fetch or fail loudly. NO synthetic fabrication, NO tenant/AOI default. RESUMABLE.

  Env: AOI_ID · TENANT · GEE_SERVICE_ACCOUNT_KEY (Vault) · GEE_PROJECT/GEE_SERVICE_ACCOUNT (opt) ·
       WINDOW_DAYS (default 10; monsoon cloud needs a wider AOD compositing window) · BATCH (cells/reduceRegions, default 500) · RESUME (1)
"""
import json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_HINT = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
PROJECT = os.environ.get("GEE_PROJECT", "")
SA_EMAIL = os.environ.get("GEE_SERVICE_ACCOUNT", "")
SA_KEY = os.environ.get("GEE_SERVICE_ACCOUNT_KEY", "")
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "10"))
BATCH = int(os.environ.get("BATCH", "500"))
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/maiac"
MAIAC = "MODIS/061/MCD19A2_GRANULES"
DERIVED = ["gee:MODIS/061/MCD19A2_GRANULES"]
AOI_REF = ""


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
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant.")
    import h3
    aoi, bbox, res, AOI_REF = resolve_aoi()
    print(f"tenant={TENANT} · AOI {AOI_REF} · bbox={bbox} · res={res} · MAIAC window {WINDOW_DAYS}d", flush=True)
    ee = init_ee()

    rect = ee.Geometry.Rectangle(bbox)
    end = ee.Date(NOW)
    start = end.advance(-WINDOW_DAYS, "day")
    col = (ee.ImageCollection(MAIAC).filterDate(start, end).filterBounds(rect).select("Optical_Depth_055"))
    n_imgs = col.size().getInfo()
    if not n_imgs:
        raise SystemExit(f"FATAL: MAIAC returned 0 granules over the AOI in the last {WINDOW_DAYS} days — nothing written (no fill).")
    aod = col.mean().multiply(0.001)                       # scale factor 0.001 → unitless AOD; masked where no valid pixel
    validfrac = aod.mask().rename("valid").unmask(0)       # 1 where a valid retrieval exists, 0 elsewhere
    img = aod.rename("aod").addBands(validfrac)
    reducer = ee.Reducer.mean().combine(ee.Reducer.stdDev(), sharedInputs=True)
    print(f"  MAIAC granules in window: {n_imgs}", flush=True)

    # AOI-level probe: is there ANY valid (cloud-free) AOD retrieval over the whole AOI this window? This
    # distinguishes genuine all-cloud (monsoon) from a band/logic bug — and tells the operator to widen the window.
    try:
        probe = img.reduceRegion(reducer=ee.Reducer.mean(), geometry=rect, scale=1000, maxPixels=int(1e10), bestEffort=True).getInfo()
        print(f"  AOI-level probe: aod_mean={probe.get('aod')} valid_fraction={probe.get('valid')}", flush=True)
    except Exception as ex:
        print(f"  AOI probe failed: {type(ex).__name__}: {str(ex)[:80]}", flush=True)

    cells = h3_cells(bbox, res)
    if os.environ.get("RESUME", "1") != "0":
        done = set()
        try:
            for r in (call("/v1/query", {"scope": "cell.aod"}).get("results") or []):
                hid = ((r.get("object") or r).get("cell_ref") or {}).get("h3_id")
                if hid:
                    done.add(hid)
        except Exception:
            pass
        if done:
            before = len(cells); cells = [c for c in cells if c not in done]
            print(f"  resuming: {len(done)} done → {len(cells)}/{before} remaining", flush=True)
    if not cells:
        print("  nothing to do — all cells already have cell.aod"); return 0

    written = failed = 0
    nb = (len(cells) + BATCH - 1) // BATCH
    for i in range(0, len(cells), BATCH):
        chunk = cells[i:i + BATCH]
        fc = ee.FeatureCollection([ee.Feature(ee.Geometry.Polygon([cell_boundary_lonlat(h)]), {"h3": h}) for h in chunk])
        red = img.reduceRegions(collection=fc, reducer=reducer, scale=1000)
        rows = None
        for attempt in range(5):
            try:
                rows = red.getInfo().get("features") or []
                break
            except Exception as ex:
                if attempt == 4:
                    print(f"  ⚠ AOD batch {i//BATCH+1}/{nb} failed ({type(ex).__name__}: {str(ex)[:60]}) — skip", flush=True)
                else:
                    time.sleep(3 * (attempt + 1))
        if rows is None:
            failed += 1
            continue
        for f in rows:
            p = f.get("properties") or {}
            h = p.get("h3")
            aod_mean = p.get("aod_mean")
            vfrac = p.get("valid_mean")
            if h is None or not isinstance(aod_mean, (int, float)):
                continue          # no valid (cloud-free) MAIAC retrieval in this cell this window → skip, don't
                                  # write a null-AOD row (missingness = absence of the fact, honest per §2)
            la, lo = h3.cell_to_latlng(h)
            fact = {
                "schema": "cell.aod", "id": f"ds:{TENANT}:cell/aod/{h}",
                "cell_ref": {"h3_id": h, "resolution": res, "centroid": [round(lo, 6), round(la, 6)],
                             "boundary": [[round(x, 6), round(y, 6)] for (x, y) in cell_boundary_lonlat(h)]},
                "aod": round(float(aod_mean), 4) if isinstance(aod_mean, (int, float)) else None,
                "aod_std": round(float(p["aod_stdDev"]), 4) if isinstance(p.get("aod_stdDev"), (int, float)) else None,
                "valid_fraction": round(float(vfrac), 3) if isinstance(vfrac, (int, float)) else None,
                "native_resolution_km": 1.0,
                "method": f"maiac-mcd19a2-{WINDOW_DAYS}d-mean", "source_ref": SRC, "layer": "L2", "confidence": 0.7,
                "derived_from": DERIVED,
                "prov": {"method": "maiac-mcd19a2", "source_ref": SRC, "confidence": 0.7, "derived_from": DERIVED,
                         "native_resolution_km": 1.0, "window_days": WINDOW_DAYS, "granules": n_imgs, "observed_at": NOW},
                "observed_at": NOW}
            fact = {k: v for k, v in fact.items() if v is not None}
            call("/v1/write", {"scope": "cell.aod", "id": fact["id"], "object": fact,
                               "valid_time": {"from": NOW}, "asserted_at": NOW})
            written += 1
        print(f"  …{min(i + BATCH, len(cells))}/{len(cells)} cells (wrote {written})", flush=True)

    if not written:
        raise SystemExit("FATAL: MAIAC returned no per-cell AOD — nothing written (no synthetic fill).")
    if failed:
        print(f"  ⚠ {failed}/{nb} AOD batches skipped — wrote {written} REAL cells (partial, no fill)", flush=True)
    print(f"wrote {written} cell.aod cells from MAIAC (real GEE, {WINDOW_DAYS}d window, {n_imgs} granules)", flush=True)
    print(f"RESULT: {json.dumps({'source': 'maiac-mcd19a2', 'granules': n_imgs, 'cells': written})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
