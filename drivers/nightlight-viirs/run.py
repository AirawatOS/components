"""Nightlight (VIIRS DNB) driver — average night-time-lights radiance per H3 cell → econ.nightlight.

Runs headless in the earth-observation runtime (GEE). Mirrors population-ghsl: read the AOI via the broker,
polyfill it to H3 cells, mean-reduce the VIIRS DNB `avg_rad` band over each cell, write one econ.nightlight per
cell. No synthetic fallback — if the GEE service-account key is missing it exits FATAL.

GEE dataset: NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG, band `avg_rad` (nW/cm2/sr), temporal mean over PERIOD.

0.2.0 — TREND (default): one econ.nightlight_trend per cell with a YEARLY series over TREND_YEARS (default 2014–2025;
VCMSLCFG starts 2014-01): each year = the MEDIAN of that year's monthly composites (robust to a stray fire / flare
month), all years reduced in ONE pass (one band per year). avg_radiance = the latest year; change_pct = the mean of
the last 3 years vs the first 3. Visualize animates the series. TREND_YEARS="" keeps the 0.1 single-PERIOD mode.
"""
import os, json, time, urllib.request, urllib.error

API   = os.environ.get("AIRAWAT_API", "http://kernel:8080")
TOKEN = os.environ.get("RUN_TOKEN", "")
TENANT = os.environ.get("TENANT")                      # no default — fail loudly
AOI_ID = os.environ.get("AOI_ID")
PERIOD = os.environ.get("PERIOD", "2023")              # calendar year of VIIRS monthlies to average
PROJECT = os.environ.get("GEE_PROJECT"); SA_EMAIL = os.environ.get("GEE_SERVICE_ACCOUNT")
SA_KEY = os.environ.get("GEE_SERVICE_ACCOUNT_KEY", "")
VIIRS_ASSET = "NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG"
SRC = "ds:airawat:ref/source/viirs-dnb"
BATCH = 200
TREND_YEARS = os.environ.get("TREND_YEARS", "2014-2025")

def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read() or b"{}")

def init_ee():
    try:
        import ee
    except ImportError:
        raise SystemExit("FATAL: earthengine-api not installed — run in the earth-observation runtime.")
    if not SA_KEY:
        raise SystemExit("FATAL: GEE_SERVICE_ACCOUNT_KEY not set — load it into Vault (Connections). No synthetic fallback.")
    key_data = open(SA_KEY).read() if (SA_KEY.endswith(".json") and os.path.exists(SA_KEY)) else SA_KEY
    kj = {}
    try: kj = json.loads(key_data)
    except Exception: pass
    email = SA_EMAIL or kj.get("client_email"); project = PROJECT or kj.get("project_id")
    if not (email and project):
        raise SystemExit("FATAL: could not resolve GEE service-account email / project from the key.")
    creds = ee.ServiceAccountCredentials(email, key_data=key_data)
    ee.Initialize(creds, project=project, opt_url="https://earthengine-highvolume.googleapis.com")
    print(f"  authenticated GEE as {email} · project {project}", flush=True)
    return ee

def get_aoi():
    rows = call("/v1/query", {"scope": "space.aoi", "limit": 50}).get("results", [])
    objs = [r.get("object", r) for r in rows]
    if AOI_ID:
        objs = [o for o in objs if o.get("id") == AOI_ID]
    if not objs:
        raise SystemExit("FATAL: no space.aoi found for this tenant — run City Setup first.")
    a = objs[0]
    return a.get("bbox"), int(a.get("h3_res", 9)), a.get("id")

def h3_cells(bbox, res):
    import h3
    w, s, e, n = bbox
    return list(h3.polygon_to_cells(h3.LatLngPoly([(s, w), (s, e), (n, e), (n, w)]), res))

def cell_boundary_lonlat(hc):
    import h3
    return [[lo, la] for (la, lo) in h3.cell_to_boundary(hc)]

def years_of(spec):
    a, _, b = spec.partition("-")
    return list(range(int(a), int(b or a) + 1))


def trend_change(series):
    """Mean of the last 3 years vs the first 3 (in %), or None. Robust to a single odd year at either end."""
    v = [s["avg_radiance"] for s in series if isinstance(s.get("avg_radiance"), (int, float))]
    if len(v) < 6:
        return None
    first, last = sum(v[:3]) / 3.0, sum(v[-3:]) / 3.0
    return round(100.0 * (last - first) / first, 1) if first > 0.5 else None    # near-dark cells: % is meaningless


def write_batch(items, now):
    r = call("/v1/write/batch", {"items": items, "asserted_at": now}) or {}
    if r.get("errors"):
        print("  batch errors (%s): %s" % (r.get("error_count", len(r["errors"])), json.dumps(r["errors"][:2])[:300]), flush=True)
    return int(r.get("written", 0))


def run_trend(ee, cells, bbox, res):
    years = years_of(TREND_YEARS)
    region = ee.Geometry.Rectangle(bbox); col = ee.ImageCollection(VIIRS_ASSET).filterBounds(region).select("avg_rad")
    bands = []
    for y in years:
        yc = col.filterDate(f"{y}-01-01", f"{y+1}-01-01")
        bands.append(ee.Image(ee.Algorithms.If(yc.size().gt(0), yc.median(), ee.Image.constant(-9999).toFloat()))
                     .rename(f"y{y}").max(-9999))
    img = ee.Image.cat(bands)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()); written = 0; batch = []
    for i in range(0, len(cells), BATCH):
        chunk = cells[i:i + BATCH]
        fc = ee.FeatureCollection([ee.Feature(ee.Geometry.Polygon([cell_boundary_lonlat(hc)]), {"h3": hc}) for hc in chunk])
        rows = None
        for attempt in range(5):
            try: rows = img.reduceRegions(collection=fc, reducer=ee.Reducer.mean(), scale=463).getInfo().get("features") or []; break
            except Exception as ex:
                if attempt == 4: print(f"  batch {i} FAILED after retries: {ex}", flush=True); rows = []
                else: time.sleep(3 * (attempt + 1))
        for f in rows:
            p = f.get("properties") or {}; hc = p.get("h3")
            series = []
            for y in years:
                v = p.get(f"y{y}")
                if isinstance(v, (int, float)) and v > -9000:
                    series.append({"t": str(y), "avg_radiance": round(max(float(v), 0.0), 3)})
            if not hc or not series:
                continue
            bnd = cell_boundary_lonlat(hc)
            cen = [round(sum(x for x, _ in bnd) / len(bnd), 6), round(sum(y for _, y in bnd) / len(bnd), 6)]
            fact = {"schema": "econ.nightlight_trend", "id": f"ds:{TENANT}:econ/nightlight_trend/{hc}", "tenant": TENANT,
                    "cell_ref": {"h3_id": hc, "centroid": cen}, "avg_radiance": series[-1]["avg_radiance"],
                    "first_year": series[0]["t"], "last_year": series[-1]["t"], "change_pct": trend_change(series),
                    "series": series, "observed_at": f"{series[-1]['t']}-07-01T00:00:00Z",
                    "prov": {"method": "viirs-dnb-vcmslcfg-yearly-median-reduceRegions", "source_ref": SRC, "asset": VIIRS_ASSET,
                             "years": f"{series[0]['t']}–{series[-1]['t']}", "confidence": 0.6, "layer": "L2", "observed_at": now}}
            batch.append({"scope": "econ.nightlight_trend", "id": fact["id"], "object": fact})
            if len(batch) >= 1000:
                written += write_batch(batch, now); batch = []
        print(f"  …{min(i + BATCH, len(cells))}/{len(cells)} cells", flush=True)
    if batch:
        written += write_batch(batch, now)
    print(f"  wrote {written} econ.nightlight_trend cells ({years[0]}–{years[-1]}, yearly median)", flush=True)
    print("RESULT: " + json.dumps({"econ_nightlight_trend": written, "years": [years[0], years[-1]]}), flush=True)
    return 0 if written else 1


def main():
    if not TOKEN or not TENANT:
        print("nightlight-viirs — needs RUN_TOKEN + TENANT (headless runtime). Handlers: ingest via main()."); return 0
    ee = init_ee()
    bbox, res, aoi_id = get_aoi()
    cells = h3_cells(bbox, res)
    print(f"  AOI {aoi_id}: {len(cells)} H3 cells at res {res}", flush=True)
    if TREND_YEARS.strip():
        return run_trend(ee, cells, bbox, res)
    start, end = f"{PERIOD}-01-01", f"{int(PERIOD)+1}-01-01"
    region = ee.Geometry.Rectangle(bbox)
    img = ee.ImageCollection(VIIRS_ASSET).filterBounds(region).filterDate(start, end).select("avg_rad").mean().unmask(0)
    obs = f"{PERIOD}-07-01T00:00:00Z"; now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    written = 0
    for i in range(0, len(cells), BATCH):
        chunk = cells[i:i + BATCH]
        fc = ee.FeatureCollection([ee.Feature(ee.Geometry.Polygon([cell_boundary_lonlat(hc)]), {"h3": hc}) for hc in chunk])
        red = img.reduceRegions(collection=fc, reducer=ee.Reducer.mean(), scale=463)
        rows = None
        for attempt in range(5):
            try: rows = red.getInfo().get("features") or []; break
            except Exception as ex:
                if attempt == 4: print(f"  batch {i} FAILED after retries: {ex}", flush=True); rows = []
                else: time.sleep(3 * (attempt + 1))
        for f in rows:
            p = f.get("properties") or {}
            hc = p.get("h3"); rad = p.get("mean")
            if hc is None or rad is None: continue
            bnd = cell_boundary_lonlat(hc)
            cen = [round(sum(x for x, _ in bnd) / len(bnd), 6), round(sum(y for _, y in bnd) / len(bnd), 6)]
            if not (bbox[0] <= cen[0] <= bbox[2] and bbox[1] <= cen[1] <= bbox[3]): continue
            fact = {"schema": "econ.nightlight", "id": f"ds:{TENANT}:econ/nightlight/{hc}", "tenant": TENANT,
                    "subject": hc, "subject_kind": "cell",
                    "space": {"h3": hc, "res": res, "centroid": cen, "boundary": [[round(x, 6), round(y, 6)] for x, y in bnd]},
                    "avg_radiance": round(float(rad), 4), "period": PERIOD, "method": "remote",
                    "source_ref": SRC, "observed_at": obs, "confidence": 0.6,
                    "prov": {"method": "viirs-dnb-monthly-mean-reduceRegions", "source_ref": SRC, "asset": VIIRS_ASSET,
                             "period": PERIOD, "confidence": 0.6, "layer": "L2", "observed_at": obs}}
            call("/v1/write", {"scope": "econ.nightlight", "id": fact["id"], "object": fact,
                               "valid_time": {"from": obs}, "asserted_at": now})
            written += 1
    print(f"  wrote {written} econ.nightlight cells", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
