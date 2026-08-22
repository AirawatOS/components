#!/usr/bin/env python3
"""rainfall-openmeteo — annual RAINFALL per H3 cell from ERA5 (Open-Meteo archive) (ADR-0060 L2, Wave-1).

Its ONLY I/O is the kernel broker:
  read   the tenant's space.aoi frame (discovered — never a hardcoded default)
  fetch  the Open-Meteo ARCHIVE API (ERA5 reanalysis), egress-gated to archive-api.open-meteo.com
  write  cell.rainfall — total annual precipitation (mm) per H3 cell

Rainfall is low spatial-frequency, so we sample a coarse GRID of points across the AOI, fetch each point's daily
precipitation for a full year in ONE archive call (comma-separated coordinates), SUM to an annual total (mm), and
assign every H3 cell the value of its NEAREST sample point. The climate sibling of the heat layer.

REAL ERA5 only — fetch or fail loudly. NO synthetic fabrication, NO tenant/AOI default. RESUMABLE (skips cells
already written).

  Env: TENANT · AOI_ID (opt; else discover) · GRID (points/side, default 7) · ARCHIVE_URL · RESUME (1)
       NORMAL_YEARS (window for the climatological NORMAL, default 10) · YEAR (optional — pin one calendar year)

DEFAULT = a climatological NORMAL: the MEAN annual rainfall over the last NORMAL_YEARS complete years (ERA5).
A baseline characterises the PLACE, so a multi-year normal is the right measure — stable, not skewed by a single
wet/dry year (a rolling-12-month total is a monitoring number, not a baseline). Pass YEAR=<yyyy> for one year.
"""
import datetime as _dt, json, math, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_HINT = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""   # NO default — never guess the tenant
GRID = int(os.environ.get("GRID", "7"))
NORMAL_YEARS = int(os.environ.get("NORMAL_YEARS", "10"))
ARCHIVE_URL = os.environ.get("ARCHIVE_URL", "https://archive-api.open-meteo.com/v1/archive")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/open-meteo"

_yr = os.environ.get("YEAR", "").strip()
if _yr:                                                          # pinned single calendar year
    _y = int(_yr)
    START, END, NYEARS, PERIOD = f"{_y}-01-01", f"{_y}-12-31", 1, str(_y)
    OBSERVED = f"{_y}-12-31T00:00:00Z"
else:                                                           # DEFAULT: N-year climatological NORMAL (mean annual)
    _end_y = _dt.datetime.now(_dt.timezone.utc).date().year - 1  # last COMPLETE calendar year
    _start_y = _end_y - NORMAL_YEARS + 1
    START, END, NYEARS = f"{_start_y}-01-01", f"{_end_y}-12-31", NORMAL_YEARS
    PERIOD = f"{_start_y}-{_end_y} normal ({NORMAL_YEARS}yr)"
    OBSERVED = f"{_end_y}-12-31T00:00:00Z"


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def unwrap(res):
    if isinstance(res, dict):
        if "object" in res and isinstance(res["object"], dict):
            return res["object"]
        for k in ("body", "data", "result", "response"):
            if k in res:
                return unwrap(res[k])
    if isinstance(res, (bytes, str)):
        try:
            return json.loads(res.decode() if isinstance(res, bytes) else res)
        except Exception:
            return res
    return res


def resolve_aoi():
    if AOI_HINT:
        ht = AOI_HINT.split(":")[1] if AOI_HINT.count(":") >= 2 else ""
        if ht and ht != TENANT:
            raise SystemExit(f"FATAL: AOI_ID {AOI_HINT} belongs to tenant '{ht}', not write tenant '{TENANT}'.")
        o = unwrap(call("/v1/read", {"scope": "space.aoi", "id": AOI_HINT}))
        if not isinstance(o, dict) or len(o.get("bbox") or []) != 4:
            raise SystemExit(f"FATAL: space.aoi {AOI_HINT} missing / no 4-el bbox: {o!r}")
        return o, [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), AOI_HINT
    rows = call("/v1/query", {"scope": "space.aoi"}).get("results") or []
    mine = [(r.get("object") or r) for r in rows
            if str((r.get("object") or r).get("id", "")).startswith(f"ds:{TENANT}:")
            and len((r.get("object") or r).get("bbox") or []) == 4]
    if not mine:
        raise SystemExit(f"FATAL: no space.aoi for tenant '{TENANT}' — run City Setup first. No default AOI.")
    o = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))
    return o, [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), o["id"]


def grid_points(bbox, k):
    w, s, e, n = bbox
    k = max(2, k)
    return [(s + (n - s) * (i + 0.5) / k, w + (e - w) * (j + 0.5) / k) for i in range(k) for j in range(k)]


def haversine(la1, lo1, la2, lo2):
    r = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fetch_annual_rainfall(pts):
    """MEAN annual precipitation (mm) at each sample point over the window, from Open-Meteo ARCHIVE (ERA5). One
    egress call (comma-separated coords); sums each point's daily precipitation over START..END and divides by the
    number of years (NYEARS) → the climatological normal (or the single-year total when YEAR is pinned). REAL or raise."""
    lats = ",".join(f"{la:.4f}" for la, lo in pts)
    lons = ",".join(f"{lo:.4f}" for la, lo in pts)
    url = (f"{ARCHIVE_URL}?latitude={lats}&longitude={lons}"
           f"&start_date={START}&end_date={END}&daily=precipitation_sum&timezone=auto")
    payload = unwrap(call("/v1/fetch", {"url": url}))
    locs = payload if isinstance(payload, list) else [payload]      # multi-location → array; single → object
    if len(locs) != len(pts):
        # Some archive deployments echo a single object for one point; guard against a shape mismatch.
        if not (len(locs) == 1 and len(pts) == 1):
            raise RuntimeError(f"archive returned {len(locs)} locations for {len(pts)} points: {str(payload)[:200]}")
    out = []
    for (la, lo), loc in zip(pts, locs):
        daily = (loc or {}).get("daily") or {}
        vals = daily.get("precipitation_sum") or []
        nums = [v for v in vals if isinstance(v, (int, float))]
        if not nums:
            raise RuntimeError(f"archive returned no precipitation for point ({la:.3f},{lo:.3f}): {str(loc)[:160]}")
        out.append((la, lo, round(sum(nums) / NYEARS, 1)))   # window total ÷ years = mean annual (the normal)
    return out


def h3_cells(bbox, res):
    import h3
    w, s, e, n = bbox
    return list(h3.polygon_to_cells(h3.LatLngPoly([(s, w), (s, e), (n, e), (n, w)]), res))


def cell_boundary_lonlat(hcell):
    import h3
    return [[lo, la] for (la, lo) in h3.cell_to_boundary(hcell)]


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant. Refusing to guess.")
    import h3
    aoi, bbox, res, aoi_ref = resolve_aoi()
    pts = grid_points(bbox, GRID)
    print(f"aoi {aoi_ref} bbox={bbox} · sampling {GRID}x{GRID}={len(pts)} ERA5 points for {PERIOD}", flush=True)
    samples = fetch_annual_rainfall(pts)   # REAL or raise
    lo_mm = min(s[2] for s in samples); hi_mm = max(s[2] for s in samples)
    print(f"  ERA5 annual rainfall across the AOI: {lo_mm:.0f}–{hi_mm:.0f} mm ({len(samples)} points)", flush=True)

    cells = h3_cells(bbox, res)
    if os.environ.get("RESUME", "1") != "0":
        done = set()
        try:
            for r in (call("/v1/query", {"scope": "cell.rainfall"}).get("results") or []):
                hid = ((r.get("object") or r).get("cell_ref") or {}).get("h3_id")
                if hid:
                    done.add(hid)
        except Exception:
            pass
        if done:
            before = len(cells); cells = [c for c in cells if c not in done]
            print(f"  resuming: {len(done)} done → {len(cells)}/{before} remaining", flush=True)
    if not cells:
        print("  nothing to do — all cells already have rainfall"); return 0

    written = 0
    for hc in cells:
        la, lo = h3.cell_to_latlng(hc)
        # nearest ERA5 sample point (rainfall is smooth over a city)
        best = min(samples, key=lambda s: haversine(la, lo, s[0], s[1]))
        rain = best[2]
        fact = {
            "schema": "cell.rainfall", "id": f"ds:{TENANT}:cell/rainfall/{hc}",
            "cell_ref": {"h3_id": hc, "resolution": res, "centroid": [round(lo, 6), round(la, 6)],
                         "boundary": [[round(x, 6), round(y, 6)] for (x, y) in cell_boundary_lonlat(hc)]},
            "rainfall_mm": rain, "period": PERIOD,
            "method": "era5-openmeteo-archive-nearest", "source_ref": SRC, "layer": "L2", "confidence": 0.85,
            "prov": {"method": "era5-openmeteo-archive", "source_ref": SRC, "period": PERIOD,
                     "grid_points": len(samples), "observed_at": OBSERVED},
            "observed_at": OBSERVED}
        call("/v1/write", {"scope": "cell.rainfall", "id": fact["id"], "object": fact,
                           "valid_time": {"from": OBSERVED}, "asserted_at": NOW})
        written += 1
        if written % 2000 == 0:
            print(f"  …wrote {written}/{len(cells)} cells", flush=True)

    if not written:
        raise SystemExit("FATAL: no cells written (no synthetic fill).")
    print(f"wrote {written} cell.rainfall cells from ERA5 (Open-Meteo archive), period {PERIOD}")
    print(f"RESULT: {json.dumps({'source': 'era5-openmeteo-archive', 'period': PERIOD, 'cells': written, 'mm_range': [lo_mm, hi_mm]})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
