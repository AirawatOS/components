#!/usr/bin/env python3
"""heat driver: the L0→L1 temperature acquisition for the City Scan (ADR-0060).

Confined (>= L1) — its ONLY I/O is the kernel broker:
  read   the space.aoi frame (the bbox to sample)
  fetch  Open-Meteo (free, keyless) for the temperature at a GRID of points in the bbox — ONE egress call
         (the API takes comma-separated coordinates and returns an array), egress-gated to api.open-meteo.com
  write  one environment.lst_observation per grid point (lat, lon, lst_value=°C, source_ref).

Open-Meteo returns 2 m AIR temperature — a keyless REAL proxy for surface heat; genuine satellite LST is a
future raster driver (noted). satellite_name records the proxy so the provenance is honest. REAL temperature
ONLY — if the egress path is unavailable the driver writes NOTHING and fails loudly. No synthetic fallback.

Env: AOI_ID · TENANT · GRID (points per side, default 6) · OPEN_METEO_URL
"""
import json, os, math, time, urllib.request, urllib.error, urllib.parse

AOI_ID = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""   # NO default — never guess the tenant
GRID = int(os.environ.get("GRID", "6"))
OPEN_METEO_URL = os.environ.get("OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast")
SOURCE_REF = "ds:airawat:ref/source/open-meteo"
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())   # real acquisition time — never a hardcoded date


def _iso(ts):
    """Normalise Open-Meteo's `current.time` (GMT, 'YYYY-MM-DDTHH:MM') to an ISO-Z instant; None → fetch time."""
    if not ts or not isinstance(ts, str):
        return NOW
    return ts if ts.endswith("Z") else (ts + ":00Z" if len(ts) == 16 else ts + "Z")


def call(path, body):
    req = urllib.request.Request(os.environ["AIRAWAT_API"] + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": os.environ["RUN_TOKEN"]}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def unwrap(res):
    if isinstance(res, dict):
        if "object" in res and isinstance(res["object"], dict):
            return res["object"]
        for k in ("body", "data", "result", "response"):
            if k in res:
                return unwrap(res[k])
        return res
    if isinstance(res, (bytes, str)):
        t = res.decode() if isinstance(res, bytes) else res
        try:
            return json.loads(t)
        except (ValueError, TypeError):
            return t
    return res


def read_aoi():
    """The AOI to sample for THIS tenant. An explicit AOI_ID is honoured only if it belongs to the write tenant;
    otherwise DISCOVER the tenant's own space.aoi. No default AOI, ever."""
    if AOI_ID:
        ht = AOI_ID.split(":")[1] if AOI_ID.count(":") >= 2 else ""
        if ht and ht != TENANT:
            raise SystemExit(f"FATAL: AOI_ID {AOI_ID} belongs to tenant '{ht}', not write tenant '{TENANT}'.")
        o = unwrap(call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}))
        if not isinstance(o, dict) or len(o.get("bbox") or []) != 4:
            raise SystemExit(f"FATAL: space.aoi {AOI_ID} missing / no 4-el bbox: {o!r}")
        return o, [float(v) for v in o["bbox"]]
    rows = call("/v1/query", {"scope": "space.aoi"}).get("results") or []
    mine = [(r.get("object") or r) for r in rows
            if str((r.get("object") or r).get("id", "")).startswith(f"ds:{TENANT}:")
            and len((r.get("object") or r).get("bbox") or []) == 4]
    if not mine:
        raise SystemExit(f"FATAL: no space.aoi for tenant '{TENANT}' — run City Setup first. No default AOI.")
    o = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))
    return o, [float(v) for v in o["bbox"]]


def grid_points(bbox, k):
    """k×k interior points across the bbox (avoid the exact edges)."""
    w, s, e, n = bbox
    pts = []
    for i in range(k):
        for j in range(k):
            fx, fy = (i + 0.5) / k, (j + 0.5) / k
            pts.append((round(s + fy * (n - s), 5), round(w + fx * (e - w), 5)))
    return pts


def _emit(idx, lat, lon, temp_c, obs):
    oid = f"ds:{TENANT}:environment/lst_observation/openmeteo-{idx}"
    fact = {"schema": "environment.lst_observation", "id": oid, "tenant": TENANT,
            "satellite_name": "open-meteo-2m-air-proxy",
            "latitude": lat, "longitude": lon, "lst_value": round(float(temp_c), 2), "unit": "celsius",
            "source": SOURCE_REF, "observed_at": obs, "layer": "L1",
            "acquired_via": "ds:airawat:driver/heat",
            # prov: the observation's provenance the house rule requires (ADR-0041/0063 R5).
            "prov": {"method": "sensor", "source": SOURCE_REF, "satellite_name": "open-meteo-2m-air-proxy",
                     "observed_at": obs}}
    call("/v1/write", {"scope": "environment.lst_observation", "id": oid, "object": fact,
                       "valid_time": {"from": obs}, "asserted_at": NOW})


def fetch_openmeteo(pts):
    lats = ",".join(str(p[0]) for p in pts); lons = ",".join(str(p[1]) for p in pts)
    url = f"{OPEN_METEO_URL}?latitude={lats}&longitude={lons}&current=temperature_2m"
    payload = unwrap(call("/v1/fetch", {"url": url}))
    arr = payload if isinstance(payload, list) else [payload]     # 1 point → object; many → array
    n = 0
    for idx, (item, p) in enumerate(zip(arr, pts)):
        cur = (item or {}).get("current") or {}
        t = cur.get("temperature_2m")
        if t is None:
            continue
        # record at OUR sample point p (Open-Meteo snaps the returned lat/lon to its coarse ~11 km grid node,
        # which can fall outside a small AOI); the value is the model's nearest estimate for that location.
        _emit(idx, p[0], p[1], t, _iso(cur.get("time"))); n += 1
    if n == 0:
        raise RuntimeError(f"open-meteo returned no temperatures: {str(payload)[:200]}")
    return n


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant. Refusing to guess.")
    aoi, bbox = read_aoi()
    pts = grid_points(bbox, GRID)
    print(f"aoi {AOI_ID} ({aoi.get('name','?')}) bbox={bbox} grid={GRID}x{GRID}={len(pts)} pts")
    # REAL temperature only — fetch or fail loudly (no synthetic fabrication).
    try:
        n = fetch_openmeteo(pts)
    except Exception as ex:
        raise SystemExit(f"FATAL: real temperature acquisition failed — writing nothing (no synthetic fallback): {ex}")
    print(f"wrote {n} environment.lst_observation points (real Open-Meteo)")
    print(f"RESULT: {json.dumps({'mode': 'openmeteo', 'written': n, 'source_ref': SOURCE_REF})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
