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
CELL_LIMIT = int(os.environ.get("CELL_LIMIT", "250"))
PER_CELL_CAP = int(os.environ.get("PER_CELL_CAP", "5000"))
FOOTPRINT_BBOX = os.environ.get("FOOTPRINT_BBOX", "").strip()
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
        out.append((g, [round(clo, 6), round(cla, 6)],
                    round(float(props["area_in_meters"]), 1) if isinstance(props.get("area_in_meters"), (int, float)) else None,
                    round(float(props["confidence"]), 3) if isinstance(props.get("confidence"), (int, float)) else None))
    if len(feats) >= PER_CELL_CAP:
        print(f"  ⚠ cell {hcell} hit the {PER_CELL_CAP}-footprint cap — dense cell, some footprints not ingested", flush=True)
    return out


def fp_id(hcell, centroid, area):
    h = hashlib.sha256(f"{centroid[0]:.6f},{centroid[1]:.6f},{area}".encode()).hexdigest()[:12]
    return f"ds:{TENANT}:building/{hcell}/{h}"


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant. Refusing to guess.")
    aoi, bbox, res, aoi_ref = resolve_aoi()
    ee = init_ee()
    cells = target_cells(bbox, res)
    if not cells:
        raise SystemExit(f"FATAL: no cell.buildings coverage for tenant '{TENANT}' — run buildings-openbuildings first.")
    done = already_done()
    todo = [(h, r) for (h, r, _cov) in cells if h not in done]
    print(f"Open Buildings v3 footprints · AOI {aoi_ref} · {len(cells)} target cells "
          f"({len(done)} already ingested) → {len(todo)} to fetch (conf≥{CONF}, cap {PER_CELL_CAP}/cell)", flush=True)
    if not todo:
        print("  nothing to do — all target cells already have footprints"); return 0

    written = cells_done = empty = 0
    for i, (hcell, cres) in enumerate(todo):
        fps = footprints_for_cell(ee, hcell, res)
        if fps is None:
            continue                                              # getInfo failed for this cell — skip, don't fabricate
        if not fps:
            empty += 1
        for (geom, centroid, area, conf) in fps:
            fact = {
                "schema": "geo.building", "id": fp_id(hcell, centroid, area), "tenant": TENANT,
                "geometry": geom, "h3_id": hcell, "resolution": res, "centroid": centroid,
                "area_m2": area, "height_m": None, "confidence": conf,
                "method": "open-buildings-v3", "source_ref": SRC, "layer": "L1", "derived_from": DERIVED,
                "source": {"aoi": aoi_ref, "driver": "buildings-footprints", "asset": OB_ASSET},
                "prov": {"method": "open-buildings-v3", "source_ref": SRC, "derived_from": DERIVED,
                         "min_footprint_confidence": CONF, "observed_at": NOW},
                "observed_at": NOW}
            call("/v1/write", {"scope": "geo.building", "id": fact["id"], "object": fact,
                               "valid_time": {"from": NOW}, "asserted_at": NOW})
            written += 1
        cells_done += 1
        if cells_done % 20 == 0 or i == len(todo) - 1:
            print(f"  …{cells_done}/{len(todo)} cells (wrote {written} footprints, {empty} empty cells)", flush=True)

    if not written:
        raise SystemExit("FATAL: Open Buildings returned no footprints for any target cell — nothing written (no synthetic fill).")
    print(f"wrote {written} geo.building footprints across {cells_done} cells from Google Open Buildings v3 (real GEE)")
    print(f"RESULT: {json.dumps({'source': 'open-buildings-v3', 'footprints': written, 'cells': cells_done, 'empty_cells': empty})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
