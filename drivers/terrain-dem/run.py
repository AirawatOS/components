#!/usr/bin/env python3
"""terrain-dem — TERRAIN from Google Earth Engine **Copernicus GLO-30 DEM** (ADR-0060 L0→L2, EO runtime).

WHY: the Open-Meteo terrain driver fetches elevation one point at a time — 100 cells/request, so a metro is
~100 rate-limited batches that stall. GEE reduces the DEM **server-side**: we ask for the mean elevation of
each H3 cell and get back only the numbers, so a whole city is a few batched calls. Runs in the certified
earth-observation runtime (same as landcover-dw). Writes the SAME geo.terrain shape the viewer already reads.

  elevation = mean of Copernicus GLO-30 (30 m) over each cell (server-side reduceRegions)
  slope     = derived from the elevation drop to ring-1 neighbours (client-side, like the Open-Meteo driver)

TRUST: real satellite DEM → geo.terrain method=`copernicus-glo30`. NO synthetic fallback. RESUMABLE — skips
cells already written so a memory-killed metro run continues on re-run.

  Env: AOI_ID · TENANT · GEE_SERVICE_ACCOUNT_KEY (Vault, materialised) · GEE_PROJECT/GEE_SERVICE_ACCOUNT (opt,
       derived from the key JSON) · BATCH (cells/reduceRegions, default 250) · RESUME (1)
"""
import json, math, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_HINT = os.environ.get("AOI_ID", "")          # the pipeline passes the exact AOI; else we discover the tenant's own
# A driver runs in the context of exactly ONE tenant, supplied by the runtime. NO default — guessing a tenant
# is how one city's data lands in another's namespace (the Kanpur-into-bmc pollution). Missing ⇒ fail loudly.
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
AOI_REF = ""                                     # the RESOLVED aoi id (set in main) — never a hardcoded default
PROJECT = os.environ.get("GEE_PROJECT", "")
SA_EMAIL = os.environ.get("GEE_SERVICE_ACCOUNT", "")
SA_KEY = os.environ.get("GEE_SERVICE_ACCOUNT_KEY", "")
BATCH = int(os.environ.get("BATCH", "250"))
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/copernicus-dem"


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def unwrap(res):
    return res.get("object") if isinstance(res, dict) and "object" in res else res


def _load_aoi(aoi_id):
    o = unwrap(call("/v1/read", {"scope": "space.aoi", "id": aoi_id}))
    if not isinstance(o, dict) or len(o.get("bbox") or []) != 4:
        raise SystemExit(f"FATAL: space.aoi {aoi_id} missing / no 4-el bbox: {o!r}")
    return o, [float(v) for v in o["bbox"]], int(o.get("h3_res", 9))


def resolve_aoi():
    """The AOI to fill for THIS tenant. An explicit AOI_ID (the pipeline passes it) is honoured ONLY if it
    belongs to the write tenant; otherwise the driver DISCOVERS the tenant's own space.aoi. No default AOI."""
    if AOI_HINT:
        ht = AOI_HINT.split(":")[1] if AOI_HINT.count(":") >= 2 else ""
        if ht and ht != TENANT:
            raise SystemExit(f"FATAL: AOI_ID {AOI_HINT} belongs to tenant '{ht}', not write tenant '{TENANT}' — "
                             f"refusing to write one city's terrain into another's namespace.")
        o, bbox, res = _load_aoi(AOI_HINT)
        return o, bbox, res, AOI_HINT
    rows = call("/v1/query", {"scope": "space.aoi"}).get("results") or []
    mine = [(r.get("object") or r) for r in rows
            if str((r.get("object") or r).get("id", "")).startswith(f"ds:{TENANT}:")
            and len((r.get("object") or r).get("bbox") or []) == 4]
    if not mine:
        raise SystemExit(f"FATAL: no space.aoi for tenant '{TENANT}' — run City Setup first. No default AOI.")
    o = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))  # the city, not a sub-AOI
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
    return [[lo, la] for (la, lo) in h3.cell_to_boundary(hcell)]   # [lng,lat] rings for ee.Geometry + the fact


def classify(slope):
    return ("flat" if slope < 2 else "gently_sloping" if slope < 5 else
            "moderate_slope" if slope < 15 else "steep" if slope < 30 else "very_steep")


def slope_deg(h, elev, res):
    import h3
    try:
        nbrs = [x for x in h3.grid_disk(h, 1) if x != h and x in elev]
    except Exception:
        nbrs = []
    if h not in elev or not nbrs:
        return 0.0
    dist = h3.average_hexagon_edge_length(res, unit="km") * 1000.0 * 1.732
    rise = max(abs(elev[h] - elev[x]) for x in nbrs)
    return round(math.degrees(math.atan2(rise, dist)), 2)


def main():
    import h3
    global AOI_REF
    if not TENANT:                                               # no tenant default — a driver never guesses its tenant
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant (the runtime "
                         "supplies it from the confined run token). Refusing to run without one.")
    aoi, bbox, res, AOI_REF = resolve_aoi()                      # honours the pipeline's AOI, else discovers the tenant's own
    name = aoi.get("name", AOI_REF.rsplit("/", 1)[-1])
    ee = init_ee()
    cells = h3_cells(bbox, res)
    print(f"Copernicus DEM · AOI '{name}' bbox={bbox} res={res} → {len(cells)} H3 cells", flush=True)

    if os.environ.get("RESUME", "1") != "0":
        done = set()
        try:
            for r in (call("/v1/query", {"scope": "geo.terrain"}).get("results") or []):
                ob = r.get("object") or r
                hid = (ob.get("space") or {}).get("h3")
                if hid:
                    done.add(hid)
        except Exception as ex:
            print(f"  (resume check skipped: {type(ex).__name__})", flush=True)
        if done:
            before = len(cells)
            cells = [c for c in cells if c not in done]
            print(f"  resuming: {len(done)} done → {len(cells)}/{before} remaining", flush=True)
    if not cells:
        print("  nothing to do — all cells already indexed"); return 0

    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30").select("DEM").mosaic()

    elev = {}; failed = 0; nb = (len(cells) + BATCH - 1) // BATCH
    for i in range(0, len(cells), BATCH):
        chunk = cells[i:i + BATCH]
        fc = ee.FeatureCollection([ee.Feature(ee.Geometry.Polygon([cell_boundary_lonlat(h)]), {"h3": h}) for h in chunk])
        red = dem.reduceRegions(collection=fc, reducer=ee.Reducer.mean(), scale=30)
        rows = None
        for attempt in range(5):
            try:
                rows = red.getInfo().get("features") or []
                break
            except Exception as ex:
                if attempt == 4:
                    print(f"  ⚠ batch {i//BATCH+1}/{nb} failed ({type(ex).__name__}: {str(ex)[:60]}) — skip", flush=True)
                else:
                    time.sleep(3 * (attempt + 1))
        if rows is None:
            failed += 1; continue
        for f in rows:
            p = f.get("properties") or {}
            m = p.get("mean")
            if isinstance(m, (int, float)):
                elev[p["h3"]] = float(m)
        print(f"  …{min(i + BATCH, len(cells))}/{len(cells)} cells (elev for {len(elev)})", flush=True)

    if not elev:
        raise SystemExit("FATAL: Copernicus DEM returned no elevations — nothing written (no synthetic fill).")

    written = 0
    for h, e in elev.items():
        la, lo = h3.cell_to_latlng(h)
        slope = slope_deg(h, elev, res)
        fact = {"schema": "geo.terrain", "id": f"ds:{TENANT}:geo/terrain/{h}",
                "space": {"h3": h, "res": res, "centroid": [round(lo, 6), round(la, 6)],
                          "boundary": [[round(x, 6), round(y, 6)] for (x, y) in cell_boundary_lonlat(h)]},
                "elevation_m": round(e, 1), "slope_deg": slope, "terrain_class": classify(slope), "unit": "m",
                "source_ref": SRC,
                # geo.terrain is a CLOSED schema: provenance lives in `prov` (component is a string), and it
                # does not accept top-level `layer`/`method`.
                "prov": {"component": "ds:airawat:driver/terrain-dem", "method": "copernicus-glo30",
                         "source_ref": SRC, "observed_at": NOW},
                "observed_at": NOW}
        call("/v1/write", {"scope": "geo.terrain", "id": fact["id"], "object": fact,
                           "valid_time": {"from": NOW}, "asserted_at": NOW})
        written += 1

    if failed:
        print(f"  ⚠ {failed}/{nb} batches skipped — wrote {written} REAL cells (partial, no fill)", flush=True)
    print(f"wrote {written} geo.terrain cells from Copernicus GLO-30 DEM (real GEE)")
    print(f"RESULT: {json.dumps({'source': 'copernicus-glo30', 'cells': written, 'batches_failed': failed})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
