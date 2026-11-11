#!/usr/bin/env python3
"""air-sensors driver (aos SDK): AOI → REAL ground-station readings → air.sensor_reading (points).

Reads the AOI (space.aoi) for the bbox, then INGESTS REAL air-quality observations from the ground-station
networks, and NEVER fabricates: if a source's key is missing/rejected or the source is unreachable it FAILS
that source (writes nothing from it); if a source simply has no stations in the AOI it reports 0. Point
measures carry the ADR-0053 space block; valid-time is the observation's own datetime.

The aos SDK owns the broker transport (tenant resolution, registry read/query/write, retry): AOI reads and
sensor-reading writes go through `ctx`. External egress (CPCB / OpenAQ public APIs) stays plain urllib; the
tenant's API keys are read from the injected env and applied exactly as before — OPENAQ_API_KEY as the
X-API-Key header, CPCB_API_KEY as the `api-key` query param.

Two REAL sources (SOURCE env selects; default "cpcb,openaq" — try each, write from all that succeed):
  • CPCB CAAQMS (data.gov.in) — India's regulatory monitoring network; the ground truth a PM2.5 model trains +
    validates against (ADR-0073).
  • OpenAQ v3 — global aggregator.

  Env: AOI_ID (opt) · SOURCE (cpcb,openaq) · MAX_LOCATIONS (OpenAQ, 25)
       AS_OF_DATE (OpenAQ historical day) · RESOURCE_ID + PAGE_LIMIT (CPCB dataset id / page size)
"""
import json, os, random, time, urllib.parse, urllib.request, urllib.error  # noqa: h3 imported lazily in run()
from datetime import datetime, timedelta, timezone
from aos import driver, ctx

OPENAQ = "https://api.openaq.org/v3"
_INJECT = {"name": "OPENAQ_API_KEY", "as": "header", "header": "X-API-Key", "prefix": ""}
_PARAMS = ("pm25", "no2", "o3", "pm10", "so2", "co")
# CPCB (data.gov.in) "Real time Air Quality Index" dataset (overridable if the portal re-keys it).
CPCB_INJECT = {"name": "CPCB_API_KEY", "as": "query", "param": "api-key"}
CPCB_POLL = {"PM2.5": "pm25", "NO2": "no2", "OZONE": "o3", "O3": "o3"}
# Structured egress identity (PLAT-EGRESS-004 — identify yourself; OpenAQ suspends anonymous/abusive clients).
_UA = "AirawatOS/air-sensors (+https://airawat.org; manishsv@gmail.com)"


def _reset_wait(hdrs, attempt):
    ra = hdrs.get("retry-after")
    if ra and str(ra).isdigit():
        return min(int(ra), 30)
    return min(2 ** attempt + random.random(), 30)         # exp backoff + jitter, capped


def _apply_inject(url, headers, inject):
    """Apply the tenant's injected secret exactly as the broker used to — header or query param."""
    if not inject:
        return url
    secret = os.environ.get(inject["name"], "")
    if not secret:
        return url
    if inject.get("as") == "header":
        headers[inject["header"]] = inject.get("prefix", "") + secret
    elif inject.get("as") == "query":
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{inject['param']}={urllib.parse.quote(secret)}"
    return url


def _fetch(url, inject=_INJECT, tries=5):
    """Direct egress with BACKOFF — a good citizen. Backs off on the VENDOR's 429 and pre-emptively pauses when
    near the rate limit. Returns (status, json|None)."""
    for attempt in range(tries):
        headers = {"User-Agent": _UA}
        u = _apply_inject(url, headers, inject)
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers=headers), timeout=60) as resp:
                st = resp.status
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
            if e.code == 429:
                wait = _reset_wait(hdrs, attempt)
                print(f"  vendor 429 → waiting {wait}s (remaining {hdrs.get('x-ratelimit-remaining', '?')})")
                time.sleep(wait); continue
            return e.code, None
        except urllib.error.URLError as e:
            print(f"  egress error → {e}"); return None, None
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
                return st, json.loads(body or "{}")
            except Exception:
                return st, None
        return st, None
    return 429, None


def _day_bounds(d):
    d0 = datetime.strptime(d, "%Y-%m-%d")
    return d0.strftime("%Y-%m-%dT00:00:00Z"), (d0 + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _fact(T, sid, lat, lng, res, vals, obs, source, station, method):
    """Build the air.sensor_reading fact (identical shape to the original write). Tenant is stamped by the SDK."""
    cell = h3.latlng_to_cell(lat, lng, res)
    rid = f"ds:{T}:air/sensor_reading/{sid}-{obs[:13]}"
    fact = {"schema": "air.sensor_reading", "id": rid, "sensor_id": sid,
            "space": {"h3": cell, "res": res, "centroid": [round(lng, 5), round(lat, 5)]},
            "pm25": vals.get("pm25"), "no2": vals.get("no2"), "o3": vals.get("o3"),
            "observed_at": obs, "source": source,
            "prov": {"method": method, "component": "ds:airawatdeveloper:driver/air-sensors", "station": station, "observed_at": obs}}
    return {k: v for k, v in fact.items() if v is not None}


# ------------------------------------------------------------------ CPCB CAAQMS (data.gov.in)
# The data.gov.in "Real time Air Quality Index" dataset reports each pollutant's AQI SUB-INDEX in avg_value, not its
# concentration (staging: Jayanagar 5th Block 213 — CPCB's dashboard showed AQI 205; PM2.5 there was ~94 µg/m³, not 213).
# Invert CPCB's National AQI breakpoints (piecewise-linear) back to µg/m³ before writing — air.sensor_reading is µg/m³.
#                 sub-index:  0   50   100  200  300  400  500
NAQI_CONC = {"pm25": (0, 30, 60, 90, 120, 250, 380),      # PM2.5 24-h, µg/m³
             "no2":  (0, 40, 80, 180, 280, 400, 520),     # NO2 24-h, µg/m³
             "o3":   (0, 50, 100, 168, 208, 748, 1000)}   # O3 8-h, µg/m³
NAQI_IDX = (0, 50, 100, 200, 300, 400, 500)


def subindex_to_conc(field, idx):
    """CPCB NAQI sub-index → concentration (µg/m³), linear within each band; beyond 500 extends the last band.
    None for an unknown pollutant or a missing / negative value (never guessed)."""
    bp = NAQI_CONC.get(field)
    if bp is None or idx is None or idx < 0:
        return None
    for i in range(1, len(NAQI_IDX)):
        if idx <= NAQI_IDX[i] or i == len(NAQI_IDX) - 1:
            lo_i, hi_i, lo_c, hi_c = NAQI_IDX[i - 1], NAQI_IDX[i], bp[i - 1], bp[i]
            return round(lo_c + (idx - lo_i) * (hi_c - lo_c) / (hi_i - lo_i), 1)
    return None


CPCB_METHOD = "cpcb-caaqms.data.gov.in (AQI sub-index → µg/m³ via CPCB NAQI breakpoints)"


def _cpcb_to_utc(last_update):
    try:
        dt = datetime.strptime(str(last_update).strip(), "%d-%m-%Y %H:%M:%S").replace(
            tzinfo=timezone(timedelta(hours=5, minutes=30)))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def ingest_cpcb(T, bbox, res, out):
    """REAL CPCB CAAQMS stations inside the AOI bbox → air.sensor_reading. Pages the all-India live dataset,
    groups rows by station, keeps those inside the bbox. api-key injected from env. Real or 0."""
    resource_id = os.environ.get("RESOURCE_ID", "3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69")
    page_limit = int(os.environ.get("PAGE_LIMIT", "1000"))
    cpcb_base = f"https://api.data.gov.in/resource/{resource_id}"
    w, s, e, n = bbox
    stations, offset, total = {}, 0, 0
    for _ in range(50):
        st, page = _fetch(f"{cpcb_base}?format=json&offset={offset}&limit={page_limit}", inject=CPCB_INJECT)
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
            conc = subindex_to_conc(field, val) if field else None
            if conc is not None:
                srec["vals"][field] = conc
        if len(recs) < page_limit:
            break
        offset += page_limit
    written = 0
    for srec in stations.values():
        if "pm25" not in srec["vals"] or not srec["obs"]:
            continue
        slug = "".join(c if c.isalnum() else "-" for c in (srec["name"] or "cpcb"))[:48].strip("-").lower()
        out.append(_fact(T, f"cpcb-{slug}", srec["lat"], srec["lon"], res, srec["vals"], srec["obs"], "cpcb",
                         srec["name"], CPCB_METHOD))
        written += 1
    print(f"  CPCB: scanned {total} rows → {len(stations)} in-AOI stations → wrote {written} with a current PM2.5")
    return written


# ------------------------------------------------------------------ OpenAQ v3
def _openaq_readings(loc):
    as_of = os.environ.get("AS_OF_DATE", "").strip()   # optional YYYY-MM-DD: ingest that day's REAL history, not /latest
    param_of = {sn.get("id"): ((sn.get("parameter") or {}).get("name") or "").lower()
                for sn in loc.get("sensors", [])}
    if not as_of:
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
    fr, to = _day_bounds(as_of)
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
    return (vals, f"{as_of}T12:00:00Z") if vals else ({}, None)


def ingest_openaq(T, bbox, res, out):
    max_locations = int(os.environ.get("MAX_LOCATIONS", "25"))
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
    for loc in results[:max_locations]:
        coords = loc.get("coordinates") or {}
        lat, lng = coords.get("latitude"), coords.get("longitude")
        if lat is None or lng is None:
            continue
        vals, obs = _openaq_readings(loc)
        if "pm25" not in vals or not obs:
            continue
        out.append(_fact(T, f"openaq-{loc['id']}", lat, lng, res, vals, obs, "openaq", loc.get("name"), "openaq.v3"))
        written += 1
    print(f"  OpenAQ: {len(results)} stations in AOI → wrote {written} with a current PM2.5")
    return written


@driver.main
def run():
    import h3                                    # lazy (heavy dep) — keeps import-time clean for aos check
    T = ctx.tenant
    if not T:
        print("FAIL: could not resolve the run tenant (/v1/context)."); return 1
    source = [s.strip().lower() for s in os.environ.get("SOURCE", "cpcb,openaq").split(",") if s.strip()]
    aoi_id = os.environ.get("AOI_ID", "")     # the injected AOI, else discovered for THIS run's tenant — NEVER a hardcoded city
    if aoi_id:
        aoi = ctx.get(aoi_id, "space.aoi")
    else:
        rows = ctx.facts("space.aoi").list()
        mine = [(r.get("object") or r) for r in rows
                if str((r.get("object") or r).get("id", "")).startswith(f"ds:{T}:")
                and len((r.get("object") or r).get("bbox") or []) == 4]
        aoi = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1]))) if mine else None
    if not aoi:
        print(f"FAIL: no space.aoi for tenant '{T}' — set AOI_ID or run City Setup first. Nothing written."); return 1
    bbox = [float(v) for v in aoi["bbox"]]; res = int(aoi["h3_res"])
    print(f"tenant={T} aoi={aoi.get('id')} bbox={bbox} · sources={source}", flush=True)

    out = []
    written = 0
    if "cpcb" in source:
        written += ingest_cpcb(T, bbox, res, out)
    if "openaq" in source:
        written += ingest_openaq(T, bbox, res, out)
    if out:
        ctx.write_many(out)

    if not written:
        print("No REAL station readings ingested from any configured source (nothing written; no synthetic fill).")
        return 1 if "cpcb" in source or "openaq" in source else 0
    print(f"ingested {written} REAL air.sensor_reading points (sources: {','.join(source)})")
    print(f"RESULT: {json.dumps({'sources': source, 'written': written})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(driver.run())
