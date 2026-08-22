#!/usr/bin/env python3
"""landcover-dw — LAND COVER from Google **Dynamic World** via the Earth Engine API (ADR-0060 L0→L2).

WHY: OSM landuse is patchy vector data and Overpass can't scale to a metro in one query. Dynamic World is a
complete, wall-to-wall **10 m** near-real-time land-cover product (Sentinel-2 + a DL model, 9 classes). Earth
Engine reduces it **server-side**, so we ask GEE for the per-class pixel histogram of EACH H3 cell and get back
only the fractions — the whole city in a few batched calls, no raw-data pull. This is the L1→L2 acquisition +
indexing FUSED: it writes cell.builtup / cell.greencover fractions directly (skipping geo.landcover + the
generic indexer).

TRUST: Dynamic World is ML-DERIVED → these cells are `ai-derived`, method=`dynamic-world` (coverage-qa reports
it as such). No synthetic fallback — if GEE is unauthenticated / unreachable, the driver FAILS LOUDLY.

CREDENTIALS (never in code): a Google **service-account** key. In production it lives in Vault at
secret/<tenant>/<component>/GEE_SERVICE_ACCOUNT_KEY and is materialised into this run's env by the launcher
(the ee client needs the raw key — the one declared exception to broker secret-injection; see SETUP.md).
  Env: GEE_PROJECT · GEE_SERVICE_ACCOUNT (email) · GEE_SERVICE_ACCOUNT_KEY (the key JSON, or a path to it)

EGRESS (declared in meta for certification/audit): earthengine.googleapis.com · oauth2.googleapis.com. In the
unsandboxed reference the ee client calls these directly; a sandboxed clean-room needs a GEE-aware broker
proxy (future) — the declaration is the honest contract in both.

  Env: AOI_ID · TENANT · DAYS (compositing window, default 365) · BATCH (cells/reduceRegions call, default 500)
       GREEN_CLASSES (default trees,grass,flooded_vegetation,shrub_and_scrub)
"""
import json, os, sys, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_HINT = os.environ.get("AOI_ID", "")          # the pipeline passes the exact AOI; else we discover the tenant's own
# A driver runs in the context of exactly ONE tenant, supplied by the runtime. NO default — guessing a tenant
# is how one city's data lands in another's namespace (the Kanpur-into-bmc pollution). Missing ⇒ fail loudly.
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
AOI_REF = ""                                     # the RESOLVED aoi id (set in main) — never a hardcoded default
PROJECT = os.environ.get("GEE_PROJECT", "")
SA_EMAIL = os.environ.get("GEE_SERVICE_ACCOUNT", "")
SA_KEY = os.environ.get("GEE_SERVICE_ACCOUNT_KEY", "")          # raw JSON or a path to the .json
DAYS = int(os.environ.get("DAYS", "365"))
BATCH = int(os.environ.get("BATCH", "250"))                    # cells per reduceRegions — smaller = lighter GEE call
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/dynamic-world"

# Dynamic World class labels (band 'label' = argmax); green = vegetation canopy classes.
DW = {0: "water", 1: "trees", 2: "grass", 3: "flooded_vegetation", 4: "crops",
      5: "shrub_and_scrub", 6: "built", 7: "bare", 8: "snow_and_ice"}
NAME2ID = {v: k for k, v in DW.items()}
GREEN = [NAME2ID[c] for c in os.environ.get("GREEN_CLASSES", "trees,grass,flooded_vegetation,shrub_and_scrub").split(",") if c.strip() in NAME2ID]
BUILT = NAME2ID["built"]


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def unwrap(res):
    return res.get("object") if isinstance(res, dict) and "object" in res else res


def write(scope, oid, obj):
    call("/v1/write", {"scope": scope, "id": oid, "object": obj,
                       "valid_time": {"from": NOW}, "asserted_at": NOW})


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
                             f"refusing to write one city's cells into another's namespace.")
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
    """Authenticate the Earth Engine client with the service account. FAILS LOUDLY if creds are absent —
    NEVER falls back to synthetic data."""
    try:
        import ee
    except ImportError:
        raise SystemExit("FATAL: earthengine-api not installed (pip install earthengine-api) — see SETUP.md")
    if not SA_KEY:
        raise SystemExit("FATAL: GEE_SERVICE_ACCOUNT_KEY not set — load the service-account key into Vault and "
                         "materialise it (see SETUP.md). No synthetic fallback.")
    key_data = open(SA_KEY).read() if (SA_KEY.endswith(".json") and os.path.exists(SA_KEY)) else SA_KEY
    kj = {}
    try:
        kj = json.loads(key_data)                            # a full SA key JSON carries client_email + project_id
    except Exception:
        pass
    email = SA_EMAIL or kj.get("client_email")               # derive from the key if not given explicitly
    project = PROJECT or kj.get("project_id")
    if not (email and project):
        raise SystemExit("FATAL: could not resolve GEE service-account email / project — provide a full "
                         "service-account key JSON (contains both), or set GEE_SERVICE_ACCOUNT + GEE_PROJECT.")
    creds = ee.ServiceAccountCredentials(email, key_data=key_data)
    ee.Initialize(creds, project=project, opt_url="https://earthengine-highvolume.googleapis.com")
    print(f"  authenticated GEE as {email} · project {project}")
    return ee


def h3_cells(bbox, res):
    import h3
    w, s, e, n = bbox
    poly = h3.LatLngPoly([(s, w), (s, e), (n, e), (n, w)])
    return list(h3.polygon_to_cells(poly, res))


def cell_boundary(hcell):
    import h3
    return [[lo, la] for (la, lo) in h3.cell_to_boundary(hcell)]   # [lng,lat] rings for ee.Geometry


def main():
    global AOI_REF
    if not TENANT:                                               # no tenant default — a driver never guesses its tenant
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant (the runtime "
                         "supplies it from the confined run token). Refusing to run without one.")
    aoi, bbox, res, AOI_REF = resolve_aoi()                      # honours the pipeline's AOI, else discovers the tenant's own
    name = aoi.get("name", AOI_REF.rsplit("/", 1)[-1])
    ee = init_ee()
    cells = h3_cells(bbox, res)
    print(f"Dynamic World · AOI '{name}' bbox={bbox} res={res} → {len(cells)} H3 cells · project={PROJECT}", flush=True)
    # RESUME: skip cells that already have a cell.builtup (deterministic ids). A big metro run can be killed
    # (memory) mid-way; each re-run then continues from where it stopped and converges to the full grid.
    if os.environ.get("RESUME", "1") != "0":
        done = set()
        try:
            for r in (call("/v1/query", {"scope": "cell.builtup"}).get("results") or []):
                ob = r.get("object") or r
                hid = (ob.get("cell_ref") or {}).get("h3_id")
                if hid:
                    done.add(hid)
        except Exception as ex:
            print(f"  (resume check skipped: {type(ex).__name__})", flush=True)
        if done:
            before = len(cells)
            cells = [h for h in cells if h not in done]
            print(f"  resuming: {len(done)} cells already done → {len(cells)}/{before} remaining", flush=True)
    if not cells:
        print("  nothing to do — all cells already indexed"); return 0

    end = time.strftime("%Y-%m-%d", time.gmtime())
    start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - DAYS * 86400))
    region = ee.Geometry.Rectangle(bbox)
    dw = ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1").filterBounds(region).filterDate(start, end)
    n_img = dw.size().getInfo()
    if not n_img:
        raise SystemExit(f"FATAL: no Dynamic World imagery for {start}..{end} over the AOI — widen DAYS. No fabricated data.")
    label = dw.select("label").mode()                              # most-frequent class per pixel over the window
    print(f"  composited {n_img} DW scenes ({start}→{end}); reducing per cell (batch={BATCH})…")

    wrote_b = wrote_g = failed = 0
    nb = (len(cells) + BATCH - 1) // BATCH
    for i in range(0, len(cells), BATCH):
        chunk = cells[i:i + BATCH]
        feats = [ee.Feature(ee.Geometry.Polygon([cell_boundary(h)]), {"h3": h}) for h in chunk]
        fc = ee.FeatureCollection(feats)
        red = label.reduceRegions(collection=fc, reducer=ee.Reducer.frequencyHistogram(), scale=10)
        rows = None
        for attempt in range(5):                                  # GEE getInfo can time out / throttle at metro scale
            try:
                rows = red.getInfo().get("features") or []
                break
            except Exception as ex:
                if attempt == 4:
                    print(f"  ⚠ batch {i//BATCH+1}/{nb} failed after retries ({type(ex).__name__}: {str(ex)[:70]}) — skip", flush=True)
                else:
                    time.sleep(3 * (attempt + 1))                 # backoff, then retry the same batch
        if rows is None:
            failed += 1; continue                                 # partial REAL data — never fabricate a cell
        for f in rows:
            props = f.get("properties") or {}
            hist = props.get("histogram") or {}
            total = sum(hist.values())
            if not total:
                continue                                          # cell had no valid pixels (cloud/edge) → skip, don't fabricate
            hc = props["h3"]
            import h3 as _h3
            la, lo = _h3.cell_to_latlng(hc)
            if not (bbox[0] <= lo <= bbox[2] and bbox[1] <= la <= bbox[3]):
                continue                                          # AOI membership guard: never write a cell outside the AOI bbox
            bound = [[round(p[0], 6), round(p[1], 6)] for p in cell_boundary(hc)]
            built = hist.get(str(BUILT), 0) / total
            green = sum(hist.get(str(c), 0) for c in GREEN) / total
            conf = round(min(0.95, 0.6 + total / 5000.0), 3)      # more valid pixels → more confident
            for scope, val in (("cell.builtup", built), ("cell.greencover", green)):
                rid = f"ds:{TENANT}:{scope.replace('.', '/')}/{hc}"
                write(scope, rid, {"schema": scope, "id": rid,
                    "cell_ref": {"h3_id": hc, "resolution": res, "centroid": [round(lo, 5), round(la, 5)], "boundary": bound},
                    "value": round(val, 4), "method": "dynamic-world", "confidence": conf,
                    "derived_from": ["gee:GOOGLE/DYNAMICWORLD/V1"], "source_ref": SRC, "layer": "L2",
                    # prov: the measure's derivation block the house rule requires (ADR-0041/0063 R5).
                    "prov": {"method": "dynamic-world", "confidence": conf, "source_ref": SRC,
                             "derived_from": ["gee:GOOGLE/DYNAMICWORLD/V1"], "observed_at": end + "T00:00:00Z"},
                    "observed_at": end + "T00:00:00Z"})
            wrote_b += 1; wrote_g += 1
        print(f"  …{min(i + BATCH, len(cells))}/{len(cells)} cells (wrote {wrote_b})", flush=True)

    if failed:
        print(f"  ⚠ {failed}/{nb} batches skipped after retries — wrote {wrote_b} REAL cells (partial, no fill)", flush=True)
    print(f"wrote {wrote_b} cell.builtup + {wrote_g} cell.greencover from Dynamic World (real GEE, ai-derived)")
    print(f"RESULT: {json.dumps({'source': 'dynamic-world', 'cells': wrote_b, 'scenes': n_img, 'batches_failed': failed})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
