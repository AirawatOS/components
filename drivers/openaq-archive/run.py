#!/usr/bin/env python3
"""openaq-archive driver: OpenAQ's public S3 archive → REAL historical hourly air.sensor_reading (points).

The live OpenAQ v3 API only serves recent measurements and is rate-limited (ADR-0073 M3.5 needs a HISTORICAL
label corpus — months of hourly PM2.5 to train + validate against, not a single snapshot). OpenAQ mirrors the
FULL measurement history to a public, anonymous S3 bucket (s3://openaq-data-archive), one gzip-CSV per station
per day. This driver walks that archive for the AOI's stations over a bounded date window and ingests every
hourly reading as air.sensor_reading — the same schema the live air-sensors driver writes, so pm25-features
treats archive + live labels identically.

  Path layout (anonymous, virtual-hosted S3):
    records/csv.gz/locationid=<id>/year=<YYYY>/month=<MM>/location-<id>-<YYYYMMDD>.csv.gz
  CSV columns (mapped by HEADER name, order-tolerant): location_id, sensors_id, location, datetime, lat, lon,
    parameter, units, value.  datetime carries a local offset (India +05:30) → normalised to UTC on write.

The gzip file is BINARY; the broker returns it base64-encoded (body_b64) — we decode → gunzip → parse CSV.
REAL archive only — a missing daily file (station offline that day) is skipped, never fabricated; if the whole
window yields nothing the run fails loudly and writes nothing (ADR: no synthetic data).

  Discovery of station ids (either):
    • LOCATION_IDS  — explicit comma list (bypasses the keyed API; use when you already know the AOI's stations)
    • else keyed OpenAQ /v3/locations?bbox=… discovery (needs OPENAQ_API_KEY; the archive itself is keyless)

  Env: TENANT/broker-context · AOI_ID (opt) · LOCATION_IDS · DATE_FROM/DATE_TO (YYYY-MM-DD) or DAYS_BACK
       · MAX_LOCATIONS (discovery cap)
"""
import base64, gzip, io, csv, json, os, random, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
import h3

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "")
OPENAQ = "https://api.openaq.org/v3"
S3_HOST = "openaq-data-archive.s3.amazonaws.com"
S3_BASE = f"https://{S3_HOST}/records/csv.gz"
_OPENAQ_INJECT = {"name": "OPENAQ_API_KEY", "as": "header", "header": "X-API-Key", "prefix": ""}
_PARAMS = ("pm25", "no2", "o3", "pm10", "so2", "co")
MAX_LOCATIONS = int(os.environ.get("MAX_LOCATIONS", "25"))
LOCATION_IDS = [s.strip() for s in os.environ.get("LOCATION_IDS", "").split(",") if s.strip()]
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
COMPONENT = "ds:airawat:driver/openaq-archive"


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def _reset_wait(hdrs, attempt):
    ra = hdrs.get("retry-after")
    if ra and str(ra).isdigit():
        return min(int(ra), 30)
    return min(2 ** attempt + random.random(), 30)


def _fetch(url, inject=None, tries=5, want_bytes=False):
    """Brokered egress. Returns (status, payload). want_bytes=True → payload is raw bytes (base64-decoded from
    the broker's body_b64, for the gzip archive files); else parsed JSON."""
    for attempt in range(tries):
        try:
            r = _call("/v1/fetch", {"url": url, **({"inject": inject} if inject else {})})
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(int(e.headers.get("Retry-After") or 2 ** attempt), 15)
                print(f"  broker rate limit → waiting {wait}s"); time.sleep(wait); continue
            return e.code, None
        st = r.get("upstream_status"); hdrs = r.get("headers") or {}
        if st == 429:
            wait = _reset_wait(hdrs, attempt)
            print(f"  vendor 429 → waiting {wait}s"); time.sleep(wait); continue
        try:
            rem = int(hdrs.get("x-ratelimit-remaining"))
            if rem <= 2:
                wait = _reset_wait(hdrs, 0)
                print(f"  approaching OpenAQ limit (remaining {rem}) → pausing {wait}s", flush=True); time.sleep(wait)
        except (TypeError, ValueError):
            pass
        if st == 200:
            if want_bytes:
                b64 = r.get("body_b64")
                if b64:
                    try:
                        return st, base64.b64decode(b64)
                    except Exception:
                        return st, None
                # some text-classified small files come back as body — still bytes-able
                body = r.get("body")
                return st, (body.encode("utf-8", "replace") if isinstance(body, str) else None)
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


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _to_utc(dt_str):
    """OpenAQ archive datetime → UTC 'YYYY-MM-DDTHH:00:00Z'. Carries a local offset (India +05:30)."""
    try:
        dt = datetime.fromisoformat(str(dt_str).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    except Exception:
        return None


def _discover(bbox):
    """Keyed OpenAQ /v3/locations discovery inside the AOI bbox → [(id, name, lat, lon)]."""
    w, s, e, n = bbox
    st, locs = _fetch(f"{OPENAQ}/locations?bbox={w},{s},{e},{n}&limit=100", inject=_OPENAQ_INJECT)
    if st != 200 or not locs:
        print(f"  discovery: /v3/locations returned {st} — set OPENAQ_API_KEY or pass LOCATION_IDS. No stations.")
        return []
    out = []
    for loc in (locs.get("results") or [])[:MAX_LOCATIONS]:
        c = loc.get("coordinates") or {}
        if c.get("latitude") is not None and c.get("longitude") is not None:
            out.append((str(loc["id"]), loc.get("name"), c["latitude"], c["longitude"]))
    return out


def _date_window():
    df, dt = os.environ.get("DATE_FROM", "").strip(), os.environ.get("DATE_TO", "").strip()
    if df and dt:
        d0, d1 = datetime.strptime(df, "%Y-%m-%d"), datetime.strptime(dt, "%Y-%m-%d")
    else:
        days = int(os.environ.get("DAYS_BACK", "90"))
        # end 3 days ago — the archive lags live by ~1-2 days; avoid chasing not-yet-uploaded files
        d1 = datetime.strptime(NOW[:10], "%Y-%m-%d") - timedelta(days=3)
        d0 = d1 - timedelta(days=days)
    days = []
    d = d0
    while d <= d1:
        days.append(d)
        d += timedelta(days=1)
    return days


def ingest_location(T, lid, res, days):
    """Walk the S3 archive for one station over the date window. Groups rows by hour → air.sensor_reading."""
    by_hour = {}   # utc_hour -> {"vals":{}, "lat":.., "lon":.., "s3":set()}
    got_files = 0
    for d in days:
        key = f"locationid={lid}/year={d.year}/month={d.month:02d}/location-{lid}-{d.strftime('%Y%m%d')}.csv.gz"
        url = f"{S3_BASE}/{key}"
        st, raw = _fetch(url, want_bytes=True)
        if st == 404 or not raw:
            continue                                    # station offline that day — skip, never fabricate
        if st != 200:
            print(f"  loc {lid} {d.date()}: archive returned {st}"); continue
        try:
            text = gzip.decompress(raw).decode("utf-8", "replace")
        except Exception as ex:
            print(f"  loc {lid} {d.date()}: gunzip failed ({ex})"); continue
        got_files += 1
        for row in csv.DictReader(io.StringIO(text)):
            pn = (row.get("parameter") or "").strip().lower()
            if pn not in _PARAMS:
                continue
            val = _num(row.get("value"))
            obs = _to_utc(row.get("datetime"))
            lat, lon = _num(row.get("lat")), _num(row.get("lon"))
            if val is None or obs is None or lat is None or lon is None:
                continue
            slot = by_hour.setdefault(obs, {"vals": {}, "lat": lat, "lon": lon, "s3": set()})
            slot["vals"][pn] = val
            slot["s3"].add(key)
    written = 0
    for obs, slot in by_hour.items():
        if "pm25" not in slot["vals"]:
            continue                                    # PM2.5 is the label — no PM2.5, no row
        lat, lon = slot["lat"], slot["lon"]
        cell = h3.latlng_to_cell(lat, lon, res)
        sid = f"openaq-{lid}"
        rid = f"ds:{T}:air/sensor_reading/{sid}-{obs[:13]}"
        fact = {"schema": "air.sensor_reading", "id": rid, "sensor_id": sid,
                "space": {"h3": cell, "res": res, "centroid": [round(lon, 5), round(lat, 5)]},
                "pm25": slot["vals"].get("pm25"), "no2": slot["vals"].get("no2"), "o3": slot["vals"].get("o3"),
                "observed_at": obs, "source": "openaq-archive",
                "prov": {"method": "openaq-s3-archive", "component": COMPONENT,
                         "source_ref": "ds:airawat:ref/source/openaq", "licence": "ODbL-1.0",
                         "s3_key": sorted(slot["s3"])[0], "observed_at": obs}}
        fact = {k: v for k, v in fact.items() if v is not None}
        _call("/v1/write", {"scope": "air.sensor_reading", "id": rid, "object": fact,
                            "valid_time": {"from": obs}, "asserted_at": NOW})
        written += 1
    print(f"  loc {lid}: {got_files}/{len(days)} daily files → {len(by_hour)} hours → wrote {written} with PM2.5", flush=True)
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
    res = int(aoi["h3_res"])
    days = _date_window()
    print(f"tenant={T} aoi={aoi.get('id')} res={res} · window={days[0].date()}..{days[-1].date()} ({len(days)}d)", flush=True)

    if LOCATION_IDS:
        stations = [(lid, None, None, None) for lid in LOCATION_IDS]
        print(f"stations: {len(stations)} from LOCATION_IDS override → {LOCATION_IDS}", flush=True)
    else:
        bbox = [float(v) for v in aoi["bbox"]]
        disc = _discover(bbox)
        stations = disc
        print(f"stations: {len(stations)} discovered in AOI bbox", flush=True)
    if not stations:
        print("FAIL: no stations to ingest (pass LOCATION_IDS or set OPENAQ_API_KEY for discovery)."); return 1

    written = 0
    for lid, _name, _lat, _lon in stations:
        written += ingest_location(T, lid, res, days)

    if not written:
        print("No REAL archive readings ingested (nothing written; no synthetic fill)."); return 1
    print(f"ingested {written} REAL historical air.sensor_reading points from the OpenAQ S3 archive")
    print(f"RESULT: {json.dumps({'stations': len(stations), 'written': written, 'window_days': len(days)})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
