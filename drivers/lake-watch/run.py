#!/usr/bin/env python3
"""Lake Watch — each lake's WATER EXTENT over the years, and the BUILDING around its shore, from satellites (GEE).

Reads the water bodies driver/osm-water wrote (geo.osm_feature, source.via = osm-water: named lakes / reservoirs /
ponds / wetlands as polygons) and measures, per water body:
  • water area per year — JRC Global Surface Water YearlyHistory 1985–2021 (Landsat, 30 m; a pixel counts as water
    if it was water in that year, seasonal or permanent), then Dynamic World 2022–2025 (Sentinel-2, 10 m; water if
    labelled water in ≥ DW_WATER_SHARE of the year's scenes). Each year says its source — the method changes in 2022.
    A year whose no-data share inside the lake exceeds MAX_NODATA (Landsat gaps, clouds) is left EMPTY, never guessed.
  • built-up share of a SHORE_RING_M ring around the shore — GHSL built surface (P2023A), epochs 1975–2020.
Writes
  water.lake       one per water body: name, kind, polygon, OSM area, the yearly series, the shore ring series, and
                   the summary (baseline 1985–89 vs the last 3 years; ring built-up 1975 vs 2020)
  water.lake_cell  one per H3 cell a water body covers: the share of the cell that was water, per year — the layer
                   Visualize animates (a lake visibly shrinking, or drying and refilling)
Remote sensing, not survey: an algal mat or water hyacinth can read as land, and a dry year as encroachment — the
caveats travel in prov. No synthetic fallback: no GEE key → FATAL.

Env: TENANT · AOI_ID (opt) · GEE_SERVICE_ACCOUNT_KEY (Vault) · GEE_PROJECT / GEE_SERVICE_ACCOUNT (opt) · MAX_LAKES ·
     MIN_LAKE_HA · SHORE_RING_M · MAX_NODATA · DW_WATER_SHARE · BATCH
"""
import json, math, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://kernel:8080")
TOKEN = os.environ.get("RUN_TOKEN", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
AOI_HINT = os.environ.get("AOI_ID", "")
PROJECT = os.environ.get("GEE_PROJECT", ""); SA_EMAIL = os.environ.get("GEE_SERVICE_ACCOUNT", "")
SA_KEY = os.environ.get("GEE_SERVICE_ACCOUNT_KEY", "")
MAX_LAKES = int(os.environ.get("MAX_LAKES", "400"))
MIN_LAKE_HA = float(os.environ.get("MIN_LAKE_HA", "1"))
SHORE_RING_M = float(os.environ.get("SHORE_RING_M", "200"))
MAX_NODATA = float(os.environ.get("MAX_NODATA", "0.3"))
DW_WATER_SHARE = float(os.environ.get("DW_WATER_SHARE", "0.25"))
BATCH = int(os.environ.get("BATCH", "20"))
VIA_WATER = "ds:airawat:driver/osm-water"
JRC = "JRC/GSW1_4/YearlyHistory"; DW = "GOOGLE/DYNAMICWORLD/V1"; GHSL = "JRC/GHSL/P2023A/GHS_BUILT_S"
FIRST_YEAR = int(os.environ.get("FIRST_YEAR", "1995"))   # 0.1.1: before ~1995 Landsat passes over India are sparse —
# one cloudy year reads as an empty lake (staging: Bellandur 1989 = 4 ha, 1991 = 31 ha between ~200 ha years)
BASE_YEARS, RECENT_YEARS = (1995, 1999), (2017, 2021)    # the change compares ONE method (JRC) at both ends
MIN_BASE_M2 = 20000                                       # a baseline under 2 ha gives meaningless % changes
JRC_YEARS = list(range(FIRST_YEAR, 2022)); DW_YEARS = list(range(2022, 2026)); GHSL_EPOCHS = [1975, 1980, 1985, 1990, 1995, 2000, 2005, 2010, 2015, 2020]
SRC = "ds:airawat:ref/source/jrc-gsw"
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
CAVEAT = ("satellite water extent: the trend and the animation are JRC Global Surface Water 1995–2021 (one method); "
          "Dynamic World 2022+ is kept per lake for reference but reads systematically lower, so it is not compared; "
          "weeds, hyacinth or algae on the water read as land, and desilting / a dry year as loss; shore built-up from GHSL (100 m)")


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=180).read() or b"{}")


def init_ee():
    try:
        import ee
    except ImportError:
        raise SystemExit("FATAL: earthengine-api not installed — this driver runs in the earth-observation runtime.")
    if not SA_KEY:
        raise SystemExit("FATAL: GEE_SERVICE_ACCOUNT_KEY not set — load it into Vault (Connections). No synthetic fallback.")
    key_data = open(SA_KEY).read() if (SA_KEY.endswith(".json") and os.path.exists(SA_KEY)) else SA_KEY
    try:
        kj = json.loads(key_data)
    except Exception:
        kj = {}
    email = SA_EMAIL or kj.get("client_email"); project = PROJECT or kj.get("project_id")
    if not (email and project):
        raise SystemExit("FATAL: could not resolve GEE service-account email / project from the key.")
    ee.Initialize(ee.ServiceAccountCredentials(email, key_data=key_data), project=project,
                  opt_url="https://earthengine-highvolume.googleapis.com")
    print(f"  authenticated GEE as {email} · project {project}", flush=True)
    return ee


def query_all(scope, filt=None, limit=2000):
    out, cur = [], None
    for _ in range(200):
        body = {"scope": scope, "limit": limit}
        if filt: body["filter"] = filt
        if cur: body["cursor"] = cur
        r = call("/v1/query", body) or {}
        out += [x.get("object", x) for x in r.get("results") or []]
        cur = r.get("next_cursor")
        if not cur: break
    return out


def get_aoi():
    objs = [o for o in query_all("space.aoi") if str(o.get("id", "")).startswith(f"ds:{TENANT}:") and len(o.get("bbox") or []) == 4]
    if AOI_HINT:
        objs = [o for o in objs if o.get("id") == AOI_HINT]
    if not objs:
        raise SystemExit("FATAL: no space.aoi for this tenant — run City Setup first.")
    a = max(objs, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))
    return [float(v) for v in a["bbox"]], int(a.get("h3_res", 9)), a["id"]


# ---------------------------------------------------------------------------------------------- pure helpers (tested)
def lake_area_ha(f):
    try:
        return float((f.get("tags") or {}).get("_area_m2") or 0) / 1e4
    except (TypeError, ValueError):
        return 0.0


def pick_lakes(feats, max_lakes=MAX_LAKES, min_ha=MIN_LAKE_HA):
    """The water bodies worth a time series: lakes, reservoirs and NAMED other water ≥ min_ha (unnamed ponds and
    wetlands are many and tiny), largest first, at most max_lakes."""
    keep = []
    for f in feats:
        g = f.get("geometry") or {}
        if g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        named = bool((f.get("tags") or {}).get("name"))
        if f.get("feature") in ("lake", "reservoir") or (named and f.get("feature") in ("water", "pond", "wetland")):
            if lake_area_ha(f) >= min_ha:
                keep.append(f)
    keep.sort(key=lambda f: -lake_area_ha(f))
    return keep[:max_lakes]


def centroid(geom):
    ring = geom["coordinates"][0] if geom["type"] == "Polygon" else max(geom["coordinates"], key=lambda p: len(p[0]))[0]
    return [round(sum(p[0] for p in ring) / len(ring), 6), round(sum(p[1] for p in ring) / len(ring), 6)]


def lake_series(props, area_m2):
    """Per-year water area from the reduced properties (w<year> water m², n<year> no-data m²): a year with more than
    MAX_NODATA of the lake unobserved is left empty (None) — a gap, not a dry lake."""
    out = []
    for y in JRC_YEARS + DW_YEARS:
        w, n = props.get(f"w{y}"), props.get(f"n{y}")
        if not isinstance(w, (int, float)):
            continue
        nod = (n / area_m2) if (isinstance(n, (int, float)) and area_m2 > 0) else 0.0
        out.append({"t": str(y), "water_area_m2": (round(w) if nod <= MAX_NODATA else None),
                    "nodata_share": round(min(max(nod, 0.0), 1.0), 3), "source": "jrc-gsw" if y <= 2021 else "dynamic-world"})
    return out


def summarise(series, ring, area_m2):
    """0.1.1: baseline = mean of the observed BASE_YEARS, recent = mean of the observed RECENT_YEARS — both JRC, so the
    change never mixes methods (Dynamic World 2022+ reads systematically lower). Change in % of the baseline, only
    when the baseline is at least MIN_BASE_M2. The ring's built-up 1975 → 2020 in points. None where inputs are missing."""
    obs = [s for s in series if isinstance(s.get("water_area_m2"), (int, float)) and s.get("source") == "jrc-gsw"]
    base = [s["water_area_m2"] for s in obs if BASE_YEARS[0] <= int(s["t"]) <= BASE_YEARS[1]]
    rec = [s for s in obs if RECENT_YEARS[0] <= int(s["t"]) <= RECENT_YEARS[1]]
    recent = [s["water_area_m2"] for s in rec]
    b = (sum(base) / len(base)) if base else None
    r = (sum(recent) / len(recent)) if recent else None
    chg = round(100.0 * (r - b) / b, 1) if (b is not None and r is not None and b >= MIN_BASE_M2) else None
    rb = {x["t"]: x["built_share"] for x in ring if isinstance(x.get("built_share"), (int, float))}
    return {"baseline_water_m2": round(b) if b is not None else None, "recent_water_m2": round(r) if r is not None else None,
            "water_change_pct": chg, "recent_years": [s["t"] for s in rec],
            "shore_built_1975": rb.get("1975"), "shore_built_2020": rb.get("2020"),
            "shore_built_change_pts": (round(100 * (rb["2020"] - rb["1975"]), 1) if "1975" in rb and "2020" in rb else None),
            "osm_area_m2": round(area_m2)}


def lake_id(f):
    return "ds:%s:water/lake/%s-%s" % (TENANT, f.get("osm_type") or "way", f.get("osm_id") or f["id"].rsplit("/", 1)[-1])


# ----------------------------------------------------------------------------------------------------- GEE images
def water_image(ee, region):
    """One image, a band pair per year: w<y> = water area (m²) per pixel, n<y> = unobserved area (m²) per pixel."""
    pa = ee.Image.pixelArea(); bands = []
    jrc = ee.ImageCollection(JRC)
    for y in JRC_YEARS:
        img = ee.Image(jrc.filterDate(f"{y}-01-01", f"{y+1}-01-01").first()).select("waterClass")
        bands += [img.gte(2).multiply(pa).rename(f"w{y}"), img.eq(0).multiply(pa).rename(f"n{y}")]
    dw = ee.ImageCollection(DW).filterBounds(region).select("label")
    for y in DW_YEARS:
        yc = dw.filterDate(f"{y}-01-01", f"{y+1}-01-01")
        share = yc.map(lambda i: i.eq(0)).mean()                     # share of the year's scenes labelled water
        seen = yc.count().gt(0)
        bands += [share.gte(DW_WATER_SHARE).And(seen).multiply(pa).unmask(0).rename(f"w{y}"),
                  seen.Not().unmask(1).multiply(pa).rename(f"n{y}")]
    return ee.Image.cat(bands)


def built_image(ee):
    col = ee.ImageCollection(GHSL)
    return ee.Image.cat([ee.Image(col.filterDate(f"{e}-01-01", f"{e+1}-01-01").first()).select("built_surface").rename(f"b{e}")
                         for e in GHSL_EPOCHS])


def reduce(ee, img, fc, scale):
    for attempt in range(5):
        try:
            return img.reduceRegions(collection=fc, reducer=ee.Reducer.sum(), scale=scale, tileScale=4).getInfo().get("features") or []
        except Exception as ex:
            if attempt == 4:
                print(f"  reduce FAILED after retries: {str(ex)[:160]}", flush=True); return []
            time.sleep(4 * (attempt + 1))


def write_batch(items):
    r = call("/v1/write/batch", {"items": items, "asserted_at": NOW}) or {}
    if r.get("errors"):
        print("  batch errors (%s): %s" % (r.get("error_count", len(r["errors"])), json.dumps(r["errors"][:2])[:300]), flush=True)
    return int(r.get("written", 0))


def main():
    if not TOKEN or not TENANT:
        print("lake-watch — needs RUN_TOKEN + TENANT (headless runtime)."); return 0
    ee = init_ee()
    import h3
    bbox, res, aoi_id = get_aoi()
    feats = query_all("geo.osm_feature", {"source.via": VIA_WATER})
    lakes = pick_lakes(feats)
    if not lakes:
        raise SystemExit("FATAL: no lakes from driver/osm-water for this tenant — run OSM Water first.")
    print(f"AOI {aoi_id}: {len(feats)} water bodies from osm-water → {len(lakes)} lakes measured "
          f"(lakes / reservoirs / named water ≥ {MIN_LAKE_HA} ha)", flush=True)
    region = ee.Geometry.Rectangle(bbox)
    wimg, bimg = water_image(ee, region), built_image(ee)
    lake_items, cell_water = [], {}          # h3 → {"lake": name, "w": {year: m²}, "n": {year: m²}}
    written = 0
    for i in range(0, len(lakes), BATCH):
        chunk = lakes[i:i + BATCH]
        polys = {f["id"]: ee.Geometry(f["geometry"]) for f in chunk}
        fc = ee.FeatureCollection([ee.Feature(polys[f["id"]], {"fid": f["id"]}) for f in chunk])
        ring = ee.FeatureCollection([ee.Feature(polys[f["id"]].buffer(SHORE_RING_M, 5).difference(polys[f["id"]], 5),
                                                {"fid": f["id"]}) for f in chunk])
        wrows = {r["properties"]["fid"]: r["properties"] for r in reduce(ee, wimg, fc, 30)}
        brows = {r["properties"]["fid"]: r["properties"] for r in reduce(ee, bimg.addBands(ee.Image.pixelArea().rename("ra")), ring, 100)}
        for f in chunk:
            p = wrows.get(f["id"]) or {}
            area = float((f.get("tags") or {}).get("_area_m2") or 0)
            series = lake_series(p, area)
            b = brows.get(f["id"]) or {}
            ra = b.get("ra") or 0
            rseries = [{"t": str(e), "built_share": round(min(b[f"b{e}"] / ra, 1.0), 3)} for e in GHSL_EPOCHS
                       if isinstance(b.get(f"b{e}"), (int, float)) and ra > 0]
            cen = centroid(f["geometry"]); name = (f.get("tags") or {}).get("name") or (f.get("tags") or {}).get("name:en")
            obj = {"schema": "water.lake", "id": lake_id(f), "tenant": TENANT, "name": name, "kind": f.get("feature"),
                   "osm_ref": f["id"], "geometry": f["geometry"],
                   "cell_ref": {"h3_id": h3.latlng_to_cell(cen[1], cen[0], res), "centroid": cen},
                   "series": series, "shore_ring_m": SHORE_RING_M, "shore_built": rseries,
                   "observed_at": NOW,
                   "prov": {"method": "jrc-gsw-yearly + dynamic-world + ghsl-built-s ring", "source_ref": SRC,
                            "derived_from": [JRC, DW, GHSL, f["id"]], "caveat": CAVEAT, "confidence": 0.7, "observed_at": NOW}}
            obj.update(summarise(series, rseries, area))
            lake_items.append({"scope": "water.lake", "id": obj["id"], "object": obj})
            # the cells this lake covers (centres inside; a small lake → its centroid cell)
            try:
                g = f["geometry"]
                parts = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
                hs = set()
                for poly in parts:
                    hs |= set(h3.polygon_to_cells(h3.LatLngPoly([(y, x) for x, y in poly[0]],
                                                                *[[(y, x) for x, y in hole] for hole in poly[1:]]), res))
                hs = hs or {obj["cell_ref"]["h3_id"]}
            except Exception:
                hs = {obj["cell_ref"]["h3_id"]}
            for h in hs:
                cell_water.setdefault(h, {"lake": name, "lake_id": obj["id"]})
        if len(lake_items) >= 200:
            written += write_batch(lake_items); lake_items = []
        print(f"  …{min(i + BATCH, len(lakes))}/{len(lakes)} lakes", flush=True)
    if lake_items:
        written += write_batch(lake_items)
    print(f"wrote {written} water.lake", flush=True)

    # per-cell water share by year (the animated layer)
    cells = sorted(cell_water); cw = 0; items = []
    wonly = wimg.select([f"w{y}" for y in JRC_YEARS] + [f"n{y}" for y in JRC_YEARS])
    for i in range(0, len(cells), 200):
        chunk = cells[i:i + 200]
        fc = ee.FeatureCollection([ee.Feature(ee.Geometry.Polygon([[[lo, la] for la, lo in h3.cell_to_boundary(h)]]), {"h3": h})
                                   for h in chunk])
        for r in reduce(ee, wonly.addBands(ee.Image.pixelArea().rename("ca")), fc, 30):
            p = r["properties"]; h = p.get("h3"); ca = p.get("ca") or 0
            if not h or ca <= 0:
                continue
            series = []
            for y in JRC_YEARS:                    # 0.1.1: one method in the animation (DW 2022+ reads ~half — a fake drop)
                w, n = p.get(f"w{y}"), p.get(f"n{y}")
                if isinstance(w, (int, float)) and not (isinstance(n, (int, float)) and n / ca > MAX_NODATA):
                    series.append({"t": str(y), "water_pct": round(100.0 * w / ca, 1)})
            if not series:
                continue
            la, lo = h3.cell_to_latlng(h)
            obj = {"schema": "water.lake_cell", "id": f"ds:{TENANT}:water/lake_cell/{h}", "tenant": TENANT,
                   "cell_ref": {"h3_id": h, "centroid": [round(lo, 6), round(la, 6)]},
                   "lake": cell_water[h]["lake"], "lake_ref": cell_water[h]["lake_id"],
                   "water_pct": series[-1]["water_pct"], "series": series, "observed_at": NOW,
                   "prov": {"method": "jrc-gsw-yearly + dynamic-world per H3 cell", "source_ref": SRC,
                            "derived_from": [JRC, DW], "caveat": CAVEAT, "confidence": 0.7, "observed_at": NOW}}
            items.append({"scope": "water.lake_cell", "id": obj["id"], "object": obj})
        if len(items) >= 1000:
            cw += write_batch(items); items = []
        print(f"  …{min(i + 200, len(cells))}/{len(cells)} lake cells", flush=True)
    if items:
        cw += write_batch(items)
    print(f"wrote {cw} water.lake_cell", flush=True)
    print("RESULT: " + json.dumps({"water_lake": written, "water_lake_cell": cw, "lakes_measured": len(lakes)}), flush=True)
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
