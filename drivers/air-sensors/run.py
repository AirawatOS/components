#!/usr/bin/env python3
"""air-sensors driver: AOI → REAL ground-station readings → air.sensor_reading (points).

Confined — its only I/O is the kernel broker. It READS the AOI (space.aoi) for the bbox, then INGESTS REAL
air-quality observations from the ground-station networks, and NEVER fabricates: if a source's key is
missing/rejected or the source is unreachable it FAILS that source (writes nothing from it); if a source simply
has no stations in the AOI it reports 0. Point measures carry the ADR-0053 space block; valid-time is the
observation's own datetime.

Two REAL sources (SOURCE env selects; default "cpcb,openaq" — try each, write from all that succeed):
  • CPCB CAAQMS (data.gov.in) — India's regulatory monitoring network; the ground truth a PM2.5 model trains +
    validates against (ADR-0073). The broker injects the tenant's CPCB_API_KEY as the `api-key` query param.
  • OpenAQ v3 — global aggregator; the broker injects OPENAQ_API_KEY as the X-API-Key header.

  Env: TENANT/broker-context · AOI_ID (opt) · SOURCE (cpcb,openaq) · MAX_LOCATIONS (OpenAQ, 25)
       AS_OF_DATE (OpenAQ historical day) · RESOURCE_ID + PAGE_LIMIT (CPCB dataset id / page size)
"""
import json, os, random, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
import h3

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "")     # the injected AOI, else discovered for THIS run's tenant — NEVER a hardcoded city
SOURCE = [s.strip().lower() for s in os.environ.get("SOURCE", "cpcb,openaq").split(",") if s.strip()]
OPENAQ = "https://api.openaq.org/v3"
MAX_LOCATIONS = int(os.environ.get("MAX_LOCATIONS", "25"))
AS_OF = os.environ.get("AS_OF_DATE", "").strip()   # optional YYYY-MM-DD: ingest that day's REAL history, not /latest
_INJECT = {"name": "OPENAQ_API_KEY", "as": "header", "header": "X-API-Key", "prefix": ""}
_PARAMS = ("pm25", "no2", "o3", "pm10", "so2", "co")
# CPCB (data.gov.in) "Real time Air Quality Index" dataset (overridable if the portal re-keys it).
RESOURCE_ID = os.environ.get("RESOURCE_ID", "3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69")
PAGE_LIMIT = int(os.environ.get("PAGE_LIMIT", "1000"))
CPCB_BASE = f"https://api.data.gov.in/resource/{RESOURCE_ID}"
CPCB_INJECT = {"name": "CPCB_API_KEY", "as": "query", "param": "api-key"}
CPCB_POLL = {"PM2.5": "pm25", "NO2": "no2", "OZONE": "o3", "O3": "o3"}
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def _reset_wait(hdrs, attempt):
    ra = hdrs.get("retry-after")
    if ra and str(ra).isdigit():
        return min(int(ra), 30)
    return min(2 ** attempt + random.random(), 30)         # exp backoff + jitter, capped


def _fetch(url, inject=_INJECT, tries=5):
    """Brokered egress with BACKOFF — a good citizen. Backs off on the BROKER's 429 and the VENDOR's 429.
    Returns (status, json|None)."""
    for attempt in range(tries):
        try:
            r = _call("/v1/fetch", {"url": url, "inject": inject})
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(int(e.headers.get("Retry-After") or 2 ** attempt), 15)
                print(f"  broker rate limit → waiting {wait}s"); time.sleep(wait); continue
            return e.code, None
        st = r.get("upstream_status"); hdrs = r.get("headers") or {}
        if st == 429:
            wait = _reset_wait(hdrs, attempt)
            print(f"  vendor 429 → waiting {wait}s (remaining {hdrs.get('x-ratelimit-remaining', '?')})")
            time.sleep(wait); continue
        # CONSIDERATE USE (OpenAQ): pre-emptively pause when we're near the limit — respect x-ratelimit-remaining
        # BEFORE the vendor has to 429 us, rather than only backing off after. Prevents overburdening the key.
        try:
            rem = int(hdrs.get("x-ratelimit-remaining"))
            if rem <= 2:
                wait = _reset_wait(hdrs, 0)
                print(f"  approaching OpenAQ limit (remaining {rem}) → pausing {wait}s", flush=True); time.sleep(wait)
        except (TypeError, ValueError):
            pass
        time.sleep(0.3)
        if st == 200:
            try:
                return st, json.loads(r.get("body") or "{}")
            except Exception:
                return st, None
        return st, None
    return 429, None


def _tenant():
    try:
        req = urllib.request.Request(API + "/v1/context", headers={"X-Run-Token": TOKEN})
        return (json.loads(urllib.request.urlopen(req, timeout=20).read()) or {}).get("tenant")
    except Exception:
        return None


def _day_bounds(d):
    d0 = datetime.strptime(d, "%Y-%m-%d")
    return d0.strftime("%Y-%m-%dT00:00:00Z"), (d0 + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _write(T, sid, lat, lng, res, vals, obs, source, station, method):
    cell = h3.latlng_to_cell(lat, lng, res)
    rid = f"ds:{T}:air/sensor_reading/{sid}-{obs[:13]}"
    fact = {"schema": "air.sensor_reading", "id": rid, "sensor_id": sid,
            "space": {"h3": cell, "res": res, "centroid": [round(lng, 5), round(lat, 5)]},
            "pm25": vals.get("pm25"), "no2": vals.get("no2"), "o3": vals.get("o3"),
            "observed_at": obs, "source": source,
            "prov": {"method": method, "component": "ds:airawat:driver/air-sensors", "station": station, "observed_at": obs}}
    fact = {k: v for k, v in fact.items() if v is not None}
    _call("/v1/write", {"scope": "air.sensor_reading", "id": rid, "object": fact,
                        "valid_time": {"from": obs}, "asserted_at": NOW})


# ------------------------------------------------------------------ CPCB CAAQMS (data.gov.in)
def _cpcb_to_utc(last_update):
    try:
        dt = datetime.strptime(str(last_update).strip(), "%d-%m-%Y %H:%M:%S").replace(
            tzinfo=timezone(timedelta(hours=5, minutes=30)))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def ingest_cpcb(T, bbox, res):
    """REAL CPCB CAAQMS stations inside the AOI bbox → air.sensor_reading. Pages the all-India live dataset,
    groups rows by station, keeps those inside the bbox. Broker injects the api-key. Real or 0."""
    w, s, e, n = bbox
    stations, offset, total = {}, 0, 0
    for _ in range(50):
        st, page = _fetch(f"{CPCB_BASE}?format=json&offset={offset}&limit={PAGE_LIMIT}", inject=CPCB_INJECT)
        if st == 403:
            print("  CPCB: data.gov.in 403 — CPCB_API_KEY missing/invalid (Connections → Air sensors → API key). Skipping CPCB.")
            return 0
        if st != 200 or not page:
            print(f"  CPCB: data.gov.in returned {st} — skipping CPCB (no synthetic fallback).")
            return 0
        recs = page.get("records") or []
        if not recs:
            break
        total += len(recs)
        for rec in recs:
            lat, lon = _num(rec.get("latitude")), _num(rec.get("longitude"))
            if lat is None or lon is None or not (s <= lat <= n and w <= lon <= e):
                continue
            field = CPCB_POLL.get(str(rec.get("pollutant_id") or "").strip().upper())
            val = _num(rec.get("avg_value") if rec.get("avg_value") is not None else rec.get("pollutant_avg"))
            key = f"{rec.get('station')}|{lat:.4f}|{lon:.4f}"
            srec = stations.setdefault(key, {"name": rec.get("station"), "lat": lat, "lon": lon,
                                             "obs": _cpcb_to_utc(rec.get("last_update")), "vals": {}})
            if field and val is not None:
                srec["vals"][field] = val
        if len(recs) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    written = 0
    for srec in stations.values():
        if "pm25" not in srec["vals"] or not srec["obs"]:
            continue
        slug = "".join(c if c.isalnum() else "-" for c in (srec["name"] or "cpcb"))[:48].strip("-").lower()
        _write(T, f"cpcb-{slug}", srec["lat"], srec["lon"], res, srec["vals"], srec["obs"], "cpcb", srec["name"],
               "cpcb-caaqms.data.gov.in")
        written += 1
    print(f"  CPCB: scanned {total} rows → {len(stations)} in-AOI stations → wrote {written} with a current PM2.5")
    return written


# ------------------------------------------------------------------ OpenAQ v3
def _openaq_readings(loc):
    param_of = {sn.get("id"): ((sn.get("parameter") or {}).get("name") or "").lower()
                for sn in loc.get("sensors", [])}
    if not AS_OF:
        st2, latest = _fetch(f"{OPENAQ}/locations/{loc['id']}/latest")
        if st2 != 200 or not latest:
            return {}, None
        vals, obs = {}, None
        for m in latest.get("results", []):
            pn = param_of.get(m.get("sensorsId"))
            if pn in _PARAMS:
                vals[pn] = m.get("value")
                obs = obs or ((m.get("datetime") or {}).get("utc"))
        return vals, obs
    fr, to = _day_bounds(AS_OF)
    vals = {}
    for sn in loc.get("sensors", []):
        pn = ((sn.get("parameter") or {}).get("name") or "").lower()
        if pn not in _PARAMS:
            continue
        st2, hist = _fetch(f"{OPENAQ}/sensors/{sn.get('id')}/measurements/hourly"
                           f"?datetime_from={fr}&datetime_to={to}&limit=168")
        if st2 != 200 or not hist:
            continue
        xs = [m.get("value") for m in hist.get("results", []) if isinstance(m.get("value"), (int, float))]
        if xs:
            vals[pn] = round(sum(xs) / len(xs), 1)
    return (vals, f"{AS_OF}T12:00:00Z") if vals else ({}, None)


def ingest_openaq(T, bbox, res):
    w, s, e, n = bbox
    st, locs = _fetch(f"{OPENAQ}/locations?bbox={w},{s},{e},{n}&limit=100")
    if st != 200:
        print(f"  OpenAQ: /locations returned {st} (set a valid OPENAQ_API_KEY). Skipping OpenAQ.")
        return 0
    results = (locs or {}).get("results", [])
    if not results:
        print(f"  OpenAQ: 0 stations in AOI bbox [{w},{s},{e},{n}].")
        return 0
    written = 0
    for loc in results[:MAX_LOCATIONS]:
        coords = loc.get("coordinates") or {}
        lat, lng = coords.get("latitude"), coords.get("longitude")
        if lat is None or lng is None:
            continue
        vals, obs = _openaq_readings(loc)
        if "pm25" not in vals or not obs:
            continue
        _write(T, f"openaq-{loc['id']}", lat, lng, res, vals, obs, "openaq", loc.get("name"), "openaq.v3")
        written += 1
    print(f"  OpenAQ: {len(results)} stations in AOI → wrote {written} with a current PM2.5")
    return written


def main():
    T = _tenant()
    if not T:
        print("FAIL: could not resolve the run tenant (/v1/context)."); return 1
    if AOI_ID:
        aoi = _call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object")
    else:
        rows = _call("/v1/query", {"scope": "space.aoi", "limit": 500}).get("results", [])
        mine = [(r.get("object") or r) for r in rows
                if str((r.get("object") or r).get("id", "")).startswith(f"ds:{T}:")
                and len((r.get("object") or r).get("bbox") or []) == 4]
        aoi = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1]))) if mine else None
    if not aoi:
        print(f"FAIL: no space.aoi for tenant '{T}' — set AOI_ID or run City Setup first. Nothing written."); return 1
    bbox = [float(v) for v in aoi["bbox"]]; res = int(aoi["h3_res"])
    print(f"tenant={T} aoi={aoi.get('id')} bbox={bbox} · sources={SOURCE}", flush=True)

    written = 0
    if "cpcb" in SOURCE:
        written += ingest_cpcb(T, bbox, res)
    if "openaq" in SOURCE:
        written += ingest_openaq(T, bbox, res)

    if not written:
        print("No REAL station readings ingested from any configured source (nothing written; no synthetic fill).")
        return 1 if "cpcb" in SOURCE or "openaq" in SOURCE else 0
    print(f"ingested {written} REAL air.sensor_reading points (sources: {','.join(SOURCE)})")
    print(f"RESULT: {json.dumps({'sources': SOURCE, 'written': written})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
