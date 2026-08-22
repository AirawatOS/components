#!/usr/bin/env python3
"""air-sensors driver: AOI → REAL ground-station readings from OpenAQ v3 → air.sensor_reading (points).

Confined — its only I/O is the kernel broker. It READS the AOI (space.aoi) for the bbox, then INGESTS
REAL air-quality observations from OpenAQ through the broker, which injects the tenant's OPENAQ_API_KEY
and audits the egress (the key never reaches this code). It does NOT fabricate data: if the key is
missing/rejected or OpenAQ is unreachable it FAILS (writes nothing, non-zero exit); if OpenAQ simply has
no stations in the AOI it reports 0 written. Point measures carry the ADR-0053 space block; valid-time is
the observation's own datetime.
"""
import json, os, random, time, urllib.request, urllib.error
import h3

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/kanpur-core")
OPENAQ = "https://api.openaq.org/v3"
MAX_LOCATIONS = int(os.environ.get("MAX_LOCATIONS", "25"))
_INJECT = {"name": "OPENAQ_API_KEY", "as": "header", "header": "X-API-Key", "prefix": ""}
_PARAMS = ("pm25", "no2", "o3", "pm10", "so2", "co")


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _reset_wait(hdrs, attempt):
    ra = hdrs.get("retry-after")
    if ra and str(ra).isdigit():
        return min(int(ra), 30)
    return min(2 ** attempt + random.random(), 30)         # exp backoff + jitter, capped


def _fetch(url, tries=5):
    """Brokered egress to OpenAQ with BACKOFF — a good citizen (OpenAQ suspends keys for repeated rate
    hits). Backs off on the BROKER's 429 (its enforced per-host limit) AND the VENDOR's 429 (respecting
    Retry-After / x-ratelimit-reset), with a small inter-call throttle. Returns (status, json|None)."""
    for attempt in range(tries):
        try:
            r = _call("/v1/fetch", {"url": url, "inject": _INJECT})
        except urllib.error.HTTPError as e:
            if e.code == 429:                              # the broker throttled us — back off, don't hammer
                wait = min(int(e.headers.get("Retry-After") or 2 ** attempt), 15)
                print(f"  broker rate limit → waiting {wait}s"); time.sleep(wait); continue
            raise
        st = r.get("upstream_status"); hdrs = r.get("headers") or {}
        if st == 429:                                      # the VENDOR rate-limited us
            wait = _reset_wait(hdrs, attempt)
            print(f"  OpenAQ 429 → waiting {wait}s (remaining {hdrs.get('x-ratelimit-remaining', '?')})")
            time.sleep(wait); continue
        time.sleep(0.3)                                    # polite spacing, well under the vendor limit
        if st == 200:
            try:
                return st, json.loads(r.get("body") or "{}")
            except Exception:
                return st, None
        return st, None
    return 429, None


def main():
    aoi = _call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object")
    if not aoi:
        print(f"FAIL: no AOI at {AOI_ID}"); return 1
    w, s, e, n = aoi["bbox"]; res = aoi["h3_res"]

    st, locs = _fetch(f"{OPENAQ}/locations?bbox={w},{s},{e},{n}&limit=100")
    if st != 200:
        print(f"FAIL: OpenAQ /locations returned {st}. Set a valid OPENAQ_API_KEY "
              f"(Components → Air sensors → Settings → API keys). Nothing written.")
        return 1
    results = (locs or {}).get("results", [])
    if not results:
        print(f"OpenAQ has 0 stations in AOI bbox [{w},{s},{e},{n}] — nothing to ingest "
              f"(try a wider AOI, or a CPCB/local-vendor driver for this district). Nothing written.")
        return 0

    written = 0
    for loc in results[:MAX_LOCATIONS]:
        coords = loc.get("coordinates") or {}
        lat, lng = coords.get("latitude"), coords.get("longitude")
        if lat is None or lng is None:
            continue
        param_of = {sn.get("id"): ((sn.get("parameter") or {}).get("name") or "").lower()
                    for sn in loc.get("sensors", [])}
        st2, latest = _fetch(f"{OPENAQ}/locations/{loc['id']}/latest")
        if st2 != 200 or not latest:
            continue
        vals, obs = {}, None
        for m in latest.get("results", []):
            pn = param_of.get(m.get("sensorsId"))
            if pn in _PARAMS:
                vals[pn] = m.get("value")
                obs = obs or ((m.get("datetime") or {}).get("utc"))
        if "pm25" not in vals or not obs:                  # need a timestamped PM2.5 to be useful
            continue
        cell = h3.latlng_to_cell(lat, lng, res)
        sid = f"openaq-{loc['id']}"
        rid = f"ds:kmc:air/sensor_reading/{sid}-{obs[:13]}"
        fact = {"schema": "air.sensor_reading", "id": rid, "sensor_id": sid,
                "space": {"h3": cell, "res": res, "centroid": [round(lng, 5), round(lat, 5)]},
                "pm25": vals.get("pm25"), "no2": vals.get("no2"), "o3": vals.get("o3"),
                "observed_at": obs, "source": "openaq",
                "prov": {"method": "openaq.v3", "component": "ds:airawat:driver/air-sensors", "station": loc.get("name")}}
        _call("/v1/write", {"scope": "air.sensor_reading", "id": rid, "object": fact,   # asserted_at = write time (not the reading time)
                            "valid_time": {"from": obs}, "asserted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        written += 1

    print(f"ingested {written} REAL air.sensor_reading points from OpenAQ "
          f"({len(results)} stations in AOI, {'' if written else 'none with a current PM2.5 reading'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
