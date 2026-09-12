#!/usr/bin/env python3
"""hydro-merit — HYDROLOGY from Google Earth Engine **MERIT Hydro** (ADR-0060 L0→L2, EO runtime).

MERIT Hydro (`MERIT/Hydro/v1_0_1`, ~90 m, Yamazaki et al. 2019) is the professional free global
hydrography. From its bands we derive the city's flood substrate WITHOUT running our own D8 flow
routing — the hard hydrology is already solved in the dataset:

  band  meaning                                        we use it for
  hnd   Height Above Nearest Drainage (HAND, metres)   flood susceptibility (low HAND ⇒ flood-prone)
  upa   upstream drainage area (km²) = flow accumulation drainage presence + the stream network
  elv   hydrologically-conditioned elevation (m)        per-cell min elevation
  wth   river channel width (m, only on channels)       (reserved)

This driver writes BOTH tiers of the plan (never H3-only — that would aggregate hydrology too early):
  • L1 native geometry  `geo.hydro_feature` — the DRAINAGE NETWORK: pixels with upa ≥ STREAM_THRESHOLD_KM2
    are vectorised (server-side reduceToVectors, clipped to the AOI) into stream polygons — the visible
    blue-line channels the H3 summary averages away.
  • L2 H3 cells         `cell.hydrology` — per cell: hand_min_m, hand_mean_m, upstream_area_max_km2,
    flood_susceptibility (0-1, from HAND) — the analytical flood layer.

TRUST: real MERIT Hydro only. NO synthetic fallback — if GEE returns nothing the driver fails loudly
and writes nothing. RESUMABLE — skips cells that already have a cell.hydrology.

flood_susceptibility is the HAND flood proxy (Nobre et al. 2011, Rennó et al. 2008): a location's
vertical distance to the nearest channel is a strong, physically-grounded predictor of inundation.
susceptibility = clamp(1 − hand_min / HAND_REF_M, 0, 1); HAND_REF_M default 25 m (≥25 m above drainage
⇒ negligible fluvial flood risk). This is a screening indicator, not an engineering flood model.

  Env: AOI_ID · TENANT · GEE_SERVICE_ACCOUNT_KEY (Vault) · GEE_PROJECT/GEE_SERVICE_ACCOUNT (opt, from key)
       BATCH (cells/reduceRegions, default 250) · RESUME (1) · STREAM_THRESHOLD_KM2 (default 2.0)
       HAND_REF_M (default 25) · DRAINAGE_MIN_KM2 (default 1.0, sets drainage_present)
"""
import json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_HINT = os.environ.get("AOI_ID", "")          # optional: the pipeline passes the exact AOI; else we discover it
# A driver runs in the context of exactly ONE tenant, supplied by the runtime (the run token is confined to it).
# There is NO default — guessing a tenant is how one city's data lands in another's namespace. Missing ⇒ fail.
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
AOI_REF = ""                                     # the RESOLVED aoi id (set in main) — never a hardcoded default
PROJECT = os.environ.get("GEE_PROJECT", "")
SA_EMAIL = os.environ.get("GEE_SERVICE_ACCOUNT", "")
SA_KEY = os.environ.get("GEE_SERVICE_ACCOUNT_KEY", "")
BATCH = int(os.environ.get("BATCH", "250"))
STREAM_THRESHOLD_KM2 = float(os.environ.get("STREAM_THRESHOLD_KM2", "2.0"))
DRAINAGE_MIN_KM2 = float(os.environ.get("DRAINAGE_MIN_KM2", "1.0"))
HAND_REF_M = float(os.environ.get("HAND_REF_M", "25"))
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/merit-hydro"
DERIVED = ["gee:MERIT/Hydro/v1_0_1"]
MERIT_ASSET = "MERIT/Hydro/v1_0_1"


def call(path, body):
    """The one and only I/O primitive: POST JSON through the kernel broker."""
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
    """Resolve the AOI to fill for THIS tenant. An explicit AOI_ID (the pipeline passes it) is honoured ONLY
    if it belongs to the write tenant — a cross-tenant AOI is refused, never silently used. With no valid hint
    the driver DISCOVERS the tenant's own space.aoi from the registry. There is no hardcoded AOI fallback."""
    if AOI_HINT:
        hint_tenant = AOI_HINT.split(":")[1] if AOI_HINT.count(":") >= 2 else ""
        if hint_tenant and hint_tenant != TENANT:
            raise SystemExit(f"FATAL: AOI_ID {AOI_HINT} belongs to tenant '{hint_tenant}', not the write tenant "
                             f"'{TENANT}'. Refusing to write one city's hydrology into another's namespace.")
        o, bbox, res = _load_aoi(AOI_HINT)
        return o, bbox, res, AOI_HINT
    rows = call("/v1/query", {"scope": "space.aoi"}).get("results") or []
    mine = [(r.get("object") or r) for r in rows
            if str((r.get("object") or r).get("id", "")).startswith(f"ds:{TENANT}:")
            and len((r.get("object") or r).get("bbox") or []) == 4]
    if not mine:
        raise SystemExit(f"FATAL: no space.aoi found for tenant '{TENANT}' — cannot fill hydrology without an AOI "
                         f"(run City Setup first). No default AOI.")
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


def flood_susceptibility(hand):
    """HAND flood proxy: a cell's typical vertical distance to the nearest drainage → 0-1 susceptibility (low HAND ⇒
    high). MUST use the cell's MEAN HAND, not the min: at res-9 nearly every 90 m cell contains one pixel next to a
    stream (HAND≈0), so min-HAND saturates the whole map at ~1.0. Mean HAND gives the real low-ground → high-ground
    gradient (Nobre et al. 2011)."""
    if hand is None:
        return None
    return round(max(0.0, min(1.0, 1.0 - (hand / HAND_REF_M))), 3)


# ---------------------------------------------------------------- L2: cell.hydrology
def index_cells(ee, bbox, res):
    import h3
    cells = h3_cells(bbox, res)
    print(f"MERIT Hydro · AOI {AOI_REF} bbox={bbox} res={res} → {len(cells)} H3 cells", flush=True)

    if os.environ.get("RESUME", "1") != "0":
        done = set()
        try:
            for r in (call("/v1/query", {"scope": "cell.hydrology"}).get("results") or []):
                ob = r.get("object") or r
                hid = (ob.get("cell_ref") or {}).get("h3_id")
                if hid:
                    done.add(hid)
        except Exception as ex:
            print(f"  (resume check skipped: {type(ex).__name__})", flush=True)
        if done:
            before = len(cells)
            cells = [c for c in cells if c not in done]
            print(f"  resuming: {len(done)} done → {len(cells)}/{before} remaining", flush=True)
    if not cells:
        print("  cell.hydrology: nothing to do — all cells already indexed", flush=True)
        return 0

    merit = ee.Image(MERIT_ASSET)
    img = (merit.select("hnd").rename("hnd")
           .addBands(merit.select("upa").rename("upa"))
           .addBands(merit.select("elv").rename("elv")))
    reducer = (ee.Reducer.min().combine(ee.Reducer.mean(), sharedInputs=True)
               .combine(ee.Reducer.max(), sharedInputs=True))

    written = failed = 0
    nb = (len(cells) + BATCH - 1) // BATCH
    for i in range(0, len(cells), BATCH):
        chunk = cells[i:i + BATCH]
        fc = ee.FeatureCollection([ee.Feature(ee.Geometry.Polygon([cell_boundary_lonlat(h)]), {"h3": h}) for h in chunk])
        red = img.reduceRegions(collection=fc, reducer=reducer, scale=90)
        rows = None
        for attempt in range(5):
            try:
                rows = red.getInfo().get("features") or []
                break
            except Exception as ex:
                if attempt == 4:
                    print(f"  ⚠ cell batch {i//BATCH+1}/{nb} failed ({type(ex).__name__}: {str(ex)[:60]}) — skip", flush=True)
                else:
                    time.sleep(3 * (attempt + 1))
        if rows is None:
            failed += 1
            continue
        for f in rows:
            p = f.get("properties") or {}
            h = p.get("h3")
            hand_min = p.get("hnd_min")
            upa_max = p.get("upa_max")
            if h is None or not isinstance(hand_min, (int, float)) or not isinstance(upa_max, (int, float)):
                continue                                          # cell had no MERIT coverage → skip, don't fabricate
            la, lo = h3_cell_latlng(h)
            if not (bbox[0] <= lo <= bbox[2] and bbox[1] <= la <= bbox[3]):
                continue                                          # AOI membership guard: never write outside the AOI bbox
            hand_mean = p.get("hnd_mean")
            fact = {
                "schema": "cell.hydrology", "id": f"ds:{TENANT}:cell/hydrology/{h}",
                "cell_ref": {"h3_id": h, "resolution": res, "centroid": [round(lo, 6), round(la, 6)],
                             "boundary": [[round(x, 6), round(y, 6)] for (x, y) in cell_boundary_lonlat(h)]},
                "hand_min_m": round(float(hand_min), 2),
                "hand_mean_m": round(float(hand_mean), 2) if isinstance(hand_mean, (int, float)) else None,
                "upstream_area_max_km2": round(float(upa_max), 4),
                "flood_susceptibility": flood_susceptibility(float(hand_mean) if isinstance(hand_mean, (int, float)) else float(hand_min)),
                "drainage_present": bool(upa_max >= DRAINAGE_MIN_KM2),
                "elevation_min_m": round(float(p["elv_min"]), 1) if isinstance(p.get("elv_min"), (int, float)) else None,
                "method": "merit-hydro-hand", "source_ref": SRC, "layer": "L2",
                "confidence": 0.8, "derived_from": DERIVED,
                "prov": {"method": "merit-hydro-hand", "confidence": 0.8, "source_ref": SRC,
                         "derived_from": DERIVED, "hand_ref_m": HAND_REF_M, "observed_at": NOW},
                "observed_at": NOW}
            fact = {k: v for k, v in fact.items() if v is not None}   # omit unset optionals (house rule R4)
            call("/v1/write", {"scope": "cell.hydrology", "id": fact["id"], "object": fact,
                               "valid_time": {"from": NOW}, "asserted_at": NOW})
            written += 1
        print(f"  …{min(i + BATCH, len(cells))}/{len(cells)} cells (wrote {written})", flush=True)

    if not written:
        raise SystemExit("FATAL: MERIT Hydro returned no cell hydrology — nothing written (no synthetic fill).")
    if failed:
        print(f"  ⚠ {failed}/{nb} cell batches skipped — wrote {written} REAL cells (partial, no fill)", flush=True)
    print(f"wrote {written} cell.hydrology cells from MERIT Hydro (real GEE)", flush=True)
    return written


def h3_cell_latlng(h):
    import h3
    return h3.cell_to_latlng(h)


# ---------------------------------------------------------------- L1: geo.hydro_feature (drainage network)
def extract_drainage(ee, bbox):
    """Vectorise the MERIT stream network (upa ≥ STREAM_THRESHOLD_KM2) inside the AOI → native geometry."""
    import h3
    rect = ee.Geometry.Rectangle(bbox)
    merit = ee.Image(MERIT_ASSET)
    upa = merit.select("upa")
    mask = upa.gte(STREAM_THRESHOLD_KM2)
    # carry upa on the vectors so each channel keeps its drainage area; reduceToVectors labels polygons.
    vec = mask.selfMask().addBands(upa).reduceToVectors(
        geometry=rect, scale=90, geometryType="polygon", eightConnected=True,
        labelProperty="stream", reducer=ee.Reducer.max(), maxPixels=1e10, bestEffort=True)
    feats = None
    for attempt in range(5):
        try:
            feats = vec.getInfo().get("features") or []
            break
        except Exception as ex:
            if attempt == 4:
                print(f"  ⚠ drainage vectorisation failed ({type(ex).__name__}: {str(ex)[:70]})", flush=True)
                return 0
            time.sleep(3 * (attempt + 1))

    written = 0
    for f in feats:
        geom = f.get("geometry")
        if not geom or not geom.get("coordinates"):
            continue
        p = f.get("properties") or {}
        upa_km2 = p.get("max") if isinstance(p.get("max"), (int, float)) else None
        # deterministic id: MERIT streams carry no external natural key, so derive a stable local id from the
        # polygon centroid's fine H3 cell (re-runs upsert the same channel rather than duplicating). The stream
        # MASK polygon is the connected drainage extent — kept as-is (consumers draw it as lines, not a filled
        # blob; see City Scan). A medial-axis "centreline" pass was tried and reverted — on blocky 90 m raster
        # polygons it fragmented into disconnected, lossy segments.
        try:
            cx, cy = _ring_centroid(geom)
            local = h3.latlng_to_cell(cy, cx, 12)
        except Exception:
            continue
        oid = f"ds:{TENANT}:geo/hydro_feature/stream/{local}"
        obj = {"schema": "geo.hydro_feature", "id": oid, "tenant": TENANT, "feature": "stream",
               "geometry": geom, "method": "merit-hydro-upa",
               "upstream_area_km2": round(float(upa_km2), 4) if upa_km2 is not None else None,
               "source": {"aoi": AOI_REF, "asset": MERIT_ASSET, "via": "ds:airawat:driver/hydro-merit",
                          "upa_threshold_km2": STREAM_THRESHOLD_KM2},
               "observed_at": NOW}
        obj = {k: v for k, v in obj.items() if v is not None}
        call("/v1/write", {"scope": "geo.hydro_feature", "id": oid, "object": obj,
                           "valid_time": {"from": NOW}, "asserted_at": NOW})
        written += 1
    print(f"wrote {written} geo.hydro_feature drainage channels (upa ≥ {STREAM_THRESHOLD_KM2} km², real MERIT Hydro)", flush=True)
    return written


def _ring_centroid(geom):
    """Mean of the first polygon ring's vertices → (lon, lat). Enough for a deterministic local id."""
    coords = geom["coordinates"]
    ring = coords[0] if geom.get("type") == "Polygon" else coords[0][0]
    xs = [pt[0] for pt in ring]
    ys = [pt[1] for pt in ring]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def main():
    global AOI_REF
    if not TENANT:                                               # no tenant default — a driver never guesses its tenant
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant (the runtime "
                         "supplies it from the confined run token). Refusing to run without one.")
    aoi, bbox, res, AOI_REF = resolve_aoi()                      # honours the pipeline's AOI, else discovers the tenant's own
    print(f"tenant={TENANT} · AOI {AOI_REF} · bbox={bbox} · res={res}", flush=True)
    ee = init_ee()

    streams = extract_drainage(ee, bbox)     # L1 native geometry first (cheap, one vector call)
    cells = index_cells(ee, bbox, res)       # L2 H3 flood substrate

    print(f"RESULT: {json.dumps({'source': 'merit-hydro', 'cell_hydrology': cells, 'drainage_features': streams})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
