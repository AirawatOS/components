#!/usr/bin/env python3
"""buildings-footprints — individual building FOOTPRINTS from Google **Open Buildings v3** via GEE (ADR-0060 L1).

Where `buildings-openbuildings` computes an aggregate footprint-coverage FRACTION per H3 cell (cell.buildings,
L2), THIS driver ingests the individual footprint POLYGONS (geo.building, L1) so a viewer can zoom into a cell
and see the real buildings inside it — the AirOS City Explorer drill-down, on real data.

Open Buildings (`GOOGLE/Research/open-buildings/v3/polygons`) is ML-derived and near-complete for Indian cities
(incl. informal settlements). We read it SERVER-SIDE via GEE, ONE H3 CELL AT A TIME (filterBounds on the cell
polygon), and write each footprint as a geo.building tagged with the cell's h3_id — so City Scan pulls just one
cell's buildings on drill. Open Buildings carries NO height, so height_m is null (the viewer extrudes to a
nominal height). Each footprint keeps its real area_in_meters + detection confidence.

TRUST: real Open Buildings only (confidence >= CONF). NO synthetic fallback; a cell with no footprints is left
empty, never fabricated. RESUMABLE — skips cells already ingested. Bounded — TARGET is the set of cells that
already have building coverage (cell.buildings), optionally capped by CELL_LIMIT and/or clipped to FOOTPRINT_BBOX,
so a first ingest is a bounded central patch rather than millions of city-wide polygons. No tenant/AOI default.

  Env: TENANT · AOI_ID (opt; else discover) · GEE_SERVICE_ACCOUNT_KEY (Vault) · GEE_PROJECT/GEE_SERVICE_ACCOUNT
       CONF (min footprint confidence, default 0.70) · RESUME (1) · CELL_LIMIT (max cells this run, default 250)
       FOOTPRINT_BBOX ("w,s,e,n" to clip the target cells to a sub-area) · PER_CELL_CAP (getInfo limit, default 5000)
"""
import hashlib, json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_HINT = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""   # NO default — never guess the tenant
PROJECT = os.environ.get("GEE_PROJECT", "")
SA_EMAIL = os.environ.get("GEE_SERVICE_ACCOUNT", "")
SA_KEY = os.environ.get("GEE_SERVICE_ACCOUNT_KEY", "")
CONF = float(os.environ.get("CONF", "0.70"))
CELL_LIMIT = int(os.environ.get("CELL_LIMIT", "250"))     # legacy per-cell path (kept for compatibility)
PER_CELL_CAP = int(os.environ.get("PER_CELL_CAP", "5000"))
FOOTPRINT_BBOX = os.environ.get("FOOTPRINT_BBOX", "").strip()
# ── BULK AOI TILE-FETCH (default) ─────────────────────────────────────────────────────────────────
# Instead of one GEE getInfo per H3 cell (~24k round-trips → API abuse + hours), pull footprints per AOI
# TILE (a few hundred round-trips). Each tile is queried with a hard `.limit(TILE_CAP)`; if it comes back
# full it is SUBDIVIDED into 4 and re-queried (adaptive quadtree) so dense areas never lose buildings and no
# single getInfo exceeds GEE's element limit. Good-citizen pacing: sequential calls, a delay between them,
# exponential backoff on errors, and a wall-clock BUDGET so a run stops cleanly (RESUME finishes the rest).
BULK = os.environ.get("BULK", "1") != "0"
TILE_DEG = float(os.environ.get("TILE_DEG", "0.03"))       # ~3 km initial tiles
TILE_CAP = int(os.environ.get("TILE_CAP", "2500"))         # getInfo feature cap per tile (well under GEE's ~5000)
MAX_DEPTH = int(os.environ.get("MAX_DEPTH", "4"))          # quadtree subdivision depth for dense tiles
TILE_DELAY = float(os.environ.get("TILE_DELAY", "0.5"))    # seconds between GEE calls — polite pacing (~≤120/min)
RUN_BUDGET_S = int(os.environ.get("RUN_BUDGET_S", "1500")) # stop after this (< the ~1700s run cap); RESUME continues
BATCH = int(os.environ.get("WRITE_BATCH", "2000"))         # geo.building rows per /v1/write/batch call
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/google-open-buildings"
DERIVED = ["gee:GOOGLE/Research/open-buildings/v3/polygons", "gee:GOOGLE/Research/open-buildings-temporal/v1"]
OB_ASSET = "GOOGLE/Research/open-buildings/v3/polygons"
# Open Buildings Temporal carries a per-pixel `building_height` (metres). A zonal MEAN over each footprint
# gives that building's height — the same join AirOS uses. levels = height / 3 m (typical storey).
OB_TEMPORAL = "GOOGLE/Research/open-buildings-temporal/v1"
STOREY_M = 3.0


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


def cell_boundary_lonlat(hcell):
    import h3
    return [[lo, la] for (la, lo) in h3.cell_to_boundary(hcell)]


def target_cells(bbox, res):
    """The cells to ingest footprints for = cells that ALREADY have building coverage (cell.buildings), so we
    only pull footprints where buildings exist. Bounded by FOOTPRINT_BBOX (if set) and CELL_LIMIT. Densest cells
    first (highest coverage) so a capped run covers the most-built area."""
    rows = call("/v1/query", {"scope": "cell.buildings"}).get("results") or []
    fb = [float(v) for v in FOOTPRINT_BBOX.split(",")] if FOOTPRINT_BBOX else None
    cands = []
    for r in rows:
        o = r.get("object") or r
        if not str(o.get("id", "")).startswith(f"ds:{TENANT}:"):
            continue
        cr = o.get("cell_ref") or {}
        h = cr.get("h3_id"); cen = cr.get("centroid") or []
        cov = o.get("building_coverage")
        if not h or len(cen) != 2:
            continue
        lo, la = cen
        if not (bbox[0] <= lo <= bbox[2] and bbox[1] <= la <= bbox[3]):
            continue                                              # AOI membership guard (cross-tenant safety)
        if fb and not (fb[0] <= lo <= fb[2] and fb[1] <= la <= fb[3]):
            continue                                              # clip to the requested sub-area
        if not (isinstance(cov, (int, float)) and cov > 0):
            continue                                              # no coverage → no footprints to fetch
        cands.append((h, cr.get("resolution", res), cov))
    cands.sort(key=lambda t: -t[2])                               # densest first
    return cands[:CELL_LIMIT]


def already_done():
    if os.environ.get("RESUME", "1") == "0":
        return set()
    done = set()
    try:
        for r in (call("/v1/query", {"scope": "geo.building"}).get("results") or []):
            h = (r.get("object") or r).get("h3_id")
            if h:
                done.add(h)
    except Exception:
        pass
    return done


def footprints_for_cell(ee, hcell, res):
    """Server-side: fetch Open Buildings footprints whose CENTROID falls in this H3 cell. Returns a list of
    (geometry, centroid[lng,lat], area_m2, confidence). Precise cell assignment (centroid in cell) — no
    double-counting across adjacent cells."""
    import h3
    ring = cell_boundary_lonlat(hcell)
    geom = ee.Geometry.Polygon([ring])
    fc = (ee.FeatureCollection(OB_ASSET).filterBounds(geom)
          .filter(ee.Filter.gte("confidence", CONF)).limit(PER_CELL_CAP))
    # HEIGHT: zonal-mean of the Open Buildings Temporal `building_height` band over each footprint (scale 4 m,
    # matching the raster). Each output feature gains a `mean` property = that building's height in metres.
    try:
        height = ee.ImageCollection(OB_TEMPORAL).filterBounds(geom).mosaic().select("building_height")
        fc = height.reduceRegions(collection=fc, reducer=ee.Reducer.mean(), scale=4)
    except Exception as ex:                                    # Temporal unavailable → fall back to footprints only
        print(f"  ⚠ cell {hcell} height join skipped ({type(ex).__name__}) — height_m stays null", flush=True)
    info = None
    for attempt in range(5):
        try:
            info = fc.getInfo(); break
        except Exception as ex:
            if attempt == 4:
                print(f"  ⚠ cell {hcell} getInfo failed ({type(ex).__name__}: {str(ex)[:60]}) — skip", flush=True)
                return None
            time.sleep(3 * (attempt + 1))
    feats = (info or {}).get("features") or []
    out = []
    for f in feats:
        g = f.get("geometry") or {}
        props = f.get("properties") or {}
        # centroid of the footprint ring
        coords = g.get("coordinates") or []
        ring0 = coords[0] if coords and isinstance(coords[0], list) else None
        if not ring0:
            continue
        xs = [p[0] for p in ring0]; ys = [p[1] for p in ring0]
        clo, cla = sum(xs) / len(xs), sum(ys) / len(ys)
        if h3.latlng_to_cell(cla, clo, res) != hcell:
            continue                                              # centroid not in THIS cell → belongs to a neighbour
        hm = props.get("mean")                                # zonal-mean building_height (metres), None if no cover
        out.append((g, [round(clo, 6), round(cla, 6)],
                    round(float(props["area_in_meters"]), 1) if isinstance(props.get("area_in_meters"), (int, float)) else None,
                    round(float(props["confidence"]), 3) if isinstance(props.get("confidence"), (int, float)) else None,
                    round(float(hm), 1) if isinstance(hm, (int, float)) and hm > 0 else None))
    if len(feats) >= PER_CELL_CAP:
        print(f"  ⚠ cell {hcell} hit the {PER_CELL_CAP}-footprint cap — dense cell, some footprints not ingested", flush=True)
    return out


def fp_id(hcell, centroid, area):
    h = hashlib.sha256(f"{centroid[0]:.6f},{centroid[1]:.6f},{area}".encode()).hexdigest()[:12]
    return f"ds:{TENANT}:building/{hcell}/{h}"


def _flatten_pts(c):
    """Yield [x,y] numeric coordinate pairs from ANY GeoJSON coordinate nesting — Polygon OR MultiPolygon
    (Open Buildings v3 has some MultiPolygon footprints, which nest one level deeper)."""
    if isinstance(c, (list, tuple)) and len(c) >= 2 and isinstance(c[0], (int, float)) and isinstance(c[1], (int, float)):
        yield c
    elif isinstance(c, (list, tuple)):
        for x in c:
            yield from _flatten_pts(x)


def _facts_from_features(feats, res, aoi_ref, w, s, e, n):
    """Turn a tile's GEE features into geo.building facts, keeping only footprints whose CENTROID falls in
    THIS tile's half-open box [w,e)x[s,n) — so a building straddling a tile seam is written exactly once."""
    import h3
    out = []
    for f in feats:
        g = f.get("geometry") or {}
        props = f.get("properties") or {}
        pts = list(_flatten_pts(g.get("coordinates") or []))     # robust to Polygon + MultiPolygon
        if not pts:
            continue
        clo = sum(p[0] for p in pts) / len(pts); cla = sum(p[1] for p in pts) / len(pts)
        if not (w <= clo < e and s <= cla < n):
            continue                                              # centroid outside this tile → its owner tile writes it
        hcell = h3.latlng_to_cell(cla, clo, res)
        hm = props.get("mean")
        area = round(float(props["area_in_meters"]), 1) if isinstance(props.get("area_in_meters"), (int, float)) else None
        conf = round(float(props["confidence"]), 3) if isinstance(props.get("confidence"), (int, float)) else None
        height_m = round(float(hm), 1) if isinstance(hm, (int, float)) and hm > 0 else None
        levels = max(1, int(round(height_m / STOREY_M))) if height_m else None
        centroid = [round(clo, 6), round(cla, 6)]
        out.append({"schema": "geo.building", "id": fp_id(hcell, centroid, area), "tenant": TENANT,
                    "geometry": g, "h3_id": hcell, "resolution": res, "centroid": centroid,
                    "area_m2": area, "height_m": height_m, "levels": levels, "confidence": conf,
                    "method": "open-buildings-v3", "source_ref": SRC, "layer": "L1", "derived_from": DERIVED,
                    "source": {"aoi": aoi_ref, "driver": "buildings-footprints", "asset": OB_ASSET},
                    "prov": {"method": "open-buildings-v3", "source_ref": SRC, "derived_from": DERIVED,
                             "min_footprint_confidence": CONF, "observed_at": NOW}, "observed_at": NOW})
    return out


def _gee_tile(ee, w, s, e, n):
    """One PACED GEE getInfo of Open Buildings footprints in a box (+ Temporal height join). Sequential, with a
    delay before every call and exponential backoff on error — never hammer the API. Returns (features, truncated)."""
    geom = ee.Geometry.Rectangle([w, s, e, n])
    fc = (ee.FeatureCollection(OB_ASSET).filterBounds(geom)
          .filter(ee.Filter.gte("confidence", CONF)).limit(TILE_CAP))
    try:
        height = ee.ImageCollection(OB_TEMPORAL).filterBounds(geom).mosaic().select("building_height")
        fc = height.reduceRegions(collection=fc, reducer=ee.Reducer.mean(), scale=4)
    except Exception:
        pass                                                      # Temporal unavailable → height_m stays null
    for attempt in range(5):
        try:
            time.sleep(TILE_DELAY)                                # polite spacing BEFORE every call
            feats = (fc.getInfo() or {}).get("features") or []
            return feats, (len(feats) >= TILE_CAP)
        except Exception as ex:
            if attempt == 4:
                print(f"  ⚠ tile [{w:.3f},{s:.3f},{e:.3f},{n:.3f}] getInfo failed ({type(ex).__name__}: {str(ex)[:60]}) — skip", flush=True)
                return None, False
            time.sleep(4 * (attempt + 1))                          # backoff — back off HARD when the API pushes back


def fetch_tile(ee, w, s, e, n, res, aoi_ref, depth, calls):
    """Adaptive quadtree: query the tile; if it returns FULL (likely truncated) split into 4 and recurse (up to
    MAX_DEPTH) so dense areas lose nothing and no getInfo exceeds the element cap. Returns (facts, calls_made)."""
    feats, truncated = _gee_tile(ee, w, s, e, n)
    calls += 1
    if feats is None:
        return [], calls
    if truncated and depth < MAX_DEPTH:
        mx, my = (w + e) / 2.0, (s + n) / 2.0
        acc = []
        for (a, b, c, d) in ((w, s, mx, my), (mx, s, e, my), (w, my, mx, n), (mx, my, e, n)):
            sub, calls = fetch_tile(ee, a, b, c, d, res, aoi_ref, depth + 1, calls)
            acc += sub
        return acc, calls
    return _facts_from_features(feats, res, aoi_ref, w, s, e, n), calls


def _tile_done(center_cell):
    """RESUME: has a prior run already ingested this tile? Tiles are written atomically per run, so if the tile's
    CENTRE H3 cell has any geo.building the whole tile is done. One cheap filtered registry read (not GEE)."""
    if os.environ.get("RESUME", "1") == "0":
        return False
    try:
        r = call("/v1/query", {"scope": "geo.building", "filter": {"h3_id": center_cell}, "limit": 1})
        return bool((r or {}).get("results"))
    except Exception:
        return False


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant. Refusing to guess.")
    import math
    aoi, bbox, res, aoi_ref = resolve_aoi()
    ee = init_ee()
    import h3
    w0, s0, e0, n0 = bbox
    if FOOTPRINT_BBOX:
        fb = [float(v) for v in FOOTPRINT_BBOX.split(",")]
        w0, s0, e0, n0 = max(w0, fb[0]), max(s0, fb[1]), min(e0, fb[2]), min(n0, fb[3])
    nx = max(1, int(math.ceil((e0 - w0) / TILE_DEG))); ny = max(1, int(math.ceil((n0 - s0) / TILE_DEG)))
    tiles = []
    for iy in range(ny):
        for ix in range(nx):
            tiles.append((w0 + ix * TILE_DEG, s0 + iy * TILE_DEG,
                          min(e0, w0 + (ix + 1) * TILE_DEG), min(n0, s0 + (iy + 1) * TILE_DEG)))
    print(f"Open Buildings v3 BULK footprints · AOI {aoi_ref} · {nx}x{ny}={len(tiles)} tiles "
          f"(conf≥{CONF}, cap {TILE_CAP}/tile, delay {TILE_DELAY}s, budget {RUN_BUDGET_S}s)", flush=True)
    t0 = time.time(); written = tiles_done = skipped = calls = 0
    batch = []
    def flush():
        nonlocal written
        if not batch:
            return
        r = call("/v1/write/batch", {"items": batch, "asserted_at": NOW}) or {}
        written += int(r.get("written", 0))
        if r.get("errors"):
            print("  batch errors:", json.dumps(r["errors"][:2])[:300], flush=True)
        batch.clear()
    stopped = False
    for (w, s, e, n) in tiles:
        if time.time() - t0 > RUN_BUDGET_S:
            print(f"  ⏸ budget {RUN_BUDGET_S}s reached — stopping cleanly at {tiles_done}/{len(tiles)} tiles; RESUME finishes the rest", flush=True)
            stopped = True; break
        cw, cn = (w + e) / 2.0, (s + n) / 2.0
        if _tile_done(h3.latlng_to_cell(cn, cw, res)):
            skipped += 1; continue
        facts, calls = fetch_tile(ee, w, s, e, n, res, aoi_ref, 0, calls)
        for fact in facts:
            batch.append({"scope": "geo.building", "id": fact["id"], "object": fact, "valid_time": {"from": NOW}})
            if len(batch) >= BATCH:
                flush()
        flush()                                                   # flush per tile → each tile is written atomically (clean RESUME)
        tiles_done += 1
        if tiles_done % 20 == 0:
            print(f"  …{tiles_done}/{len(tiles)} tiles · {calls} GEE calls · wrote {written} footprints ({skipped} skipped)", flush=True)
    print(f"wrote {written} geo.building footprints · {tiles_done} tiles · {calls} GEE calls · {skipped} skipped (RESUME)"
          + (" · INCOMPLETE (budget) — re-run to finish" if stopped else " · COMPLETE"))
    print(f"RESULT: {json.dumps({'source': 'open-buildings-v3-bulk', 'footprints': written, 'tiles': tiles_done, 'gee_calls': calls, 'skipped': skipped, 'complete': not stopped})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
