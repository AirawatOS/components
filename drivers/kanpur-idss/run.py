#!/usr/bin/env python3
"""kanpur-idss driver: Kanpur iDSS low-cost sensor network → air.sensor_reading (points).

Confined — its only I/O is the kernel broker. The iDSS API is district-scoped and LOGIN-authed: the BROKER
performs the login (username/password from Vault) and injects the JWT per this driver's egress auth profile,
so this code never sees the credentials or the token. It fetches sensor metadata + a readings window
(paginated), joins them, and writes air.sensor_reading. Two modes: LIVE (the most recent reading per sensor
in the last IDSS_WINDOW_HOURS) or, when AS_OF_DATE=YYYY-MM-DD is set, the DAILY MEAN per sensor for that
historical day. It writes into whichever tenant RUNS it (broker context) and cells at that tenant's AOI
resolution — never a hardcoded city. It does NOT fabricate data: if the API is unreachable, auth fails, or
there are no located sensors it writes nothing and says so (non-zero exit on failure).
"""
import json, os, re, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone
import h3

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "")      # explicit AOI, else discovered for THIS run's tenant — NEVER a hardcoded city
IDSS = os.environ.get("IDSS_BASE", "http://13.205.88.131:3001")
DISTRICT = os.environ.get("IDSS_DISTRICT", "uttar-pradesh-kanpur")
WINDOW_H = int(os.environ.get("IDSS_WINDOW_HOURS", "6"))
AS_OF = os.environ.get("AS_OF_DATE", "").strip()          # YYYY-MM-DD: that whole day's REAL readings (daily mean/sensor)
PM_MAX = float(os.environ.get("PM_MAX_PLAUSIBLE", "985"))  # low-cost sensors emit 0 (offline) / 999,1000 (out-of-range) as
_PARAMS = ("pm25", "no2", "o3")                            # faults — reject them (QC, not fabrication) so a dead sensor
                                                          # never becomes a false hotspot. Real extreme days stay <985.


def _ok(param, v):
    """A plausible ambient reading — drops device faults (offline 0, sentinel 999/1000)."""
    if v is None:
        return False
    if param in ("pm25", "pm10") and (v <= 0 or v >= PM_MAX):
        return False
    return True


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=40).read())


def _fetch(url):
    """Brokered GET — the broker logs in + injects the iDSS bearer (auth profile). Returns (status, json|None).
    A broker/vendor error (e.g. 403 when no IDSS credentials are bound) is returned as a code, not a crash."""
    try:
        r = _call("/v1/fetch", {"url": url})
    except urllib.error.HTTPError as e:
        return e.code, None
    st = r.get("upstream_status")
    if st == 200:
        try:
            return 200, json.loads(r.get("body") or "{}")
        except Exception:
            return 200, None
    return st, None


def _tenant():
    """This run's tenant (broker context) — the driver writes into whoever runs it, never kmc."""
    try:
        req = urllib.request.Request(API + "/v1/context", headers={"X-Run-Token": TOKEN})
        return (json.loads(urllib.request.urlopen(req, timeout=20).read()) or {}).get("tenant")
    except Exception:
        return None


def _slug(s):
    return re.sub(r"[^a-z0-9_:-]", "-", str(s or "").lower())


def _isoz(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ts


def main():
    T = _tenant()
    if not T:
        print("FAIL: could not resolve the run tenant (/v1/context)."); return 1
    if AOI_ID:                                              # explicit AOI (must belong to this tenant)
        aoi = _call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object")
    else:                                                  # discover the tenant's largest AOI (no hardcoded city)
        rows = _call("/v1/query", {"scope": "space.aoi", "limit": 500}).get("results", [])
        mine = [(r.get("object") or r) for r in rows
                if str((r.get("object") or r).get("id", "")).startswith(f"ds:{T}:")
                and len((r.get("object") or r).get("bbox") or []) == 4]
        aoi = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1]))) if mine else None
    if not aoi:
        print(f"FAIL: no space.aoi for tenant '{T}' — set AOI_ID or run City Setup first. Nothing written."); return 1
    res = int(aoi.get("h3_res") or 8)

    st, sj = _fetch(f"{IDSS}/sensors?districtId={urllib.parse.quote(DISTRICT)}")
    if st != 200 or not sj:
        print(f"FAIL: iDSS /sensors returned {st}. Set IDSS_USERNAME/IDSS_PASSWORD at install and confirm the "
              f"host is reachable. Nothing written.")
        return 1
    sensors = {}
    for s in sj.get("sensors", []):
        loc = s.get("location") or {}
        if loc.get("lat") is not None and loc.get("lng") is not None:
            sensors[s["id"]] = {"lat": float(loc["lat"]), "lng": float(loc["lng"]),
                                "name": (s.get("name") or "").strip().strip('"')}
    if not sensors:
        print("iDSS returned 0 located sensors — nothing to ingest. Nothing written."); return 0

    if AS_OF:                                              # historical: the whole requested day
        start = datetime.strptime(AS_OF, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = start + timedelta(days=1)
    else:                                                 # live: the last WINDOW_H hours
        end = datetime.now(timezone.utc); start = end - timedelta(hours=WINDOW_H)
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    page, rows = 1, []
    while True:
        q = urllib.parse.urlencode({"districtId": DISTRICT, "start_date": fmt(start), "end_date": fmt(end),
                                    "page": page, "limit": 500})
        st, bj = _fetch(f"{IDSS}/sensors/data?{q}")
        if st != 200 or not bj:
            if page == 1:
                print(f"FAIL: iDSS /sensors/data returned {st}. Nothing written."); return 1
            break
        rows.extend(bj.get("readings", []))
        tp = (bj.get("pagination") or {}).get("totalPages", 1) or 1
        if page >= tp or not bj.get("readings"):
            break
        page += 1

    if AS_OF:                                              # daily MEAN per sensor across the day
        agg = {}
        for rd in rows:
            sid = rd.get("sensorId")
            if sid not in sensors:
                continue
            for f in _PARAMS:
                try: v = float(rd.get(f)) if rd.get(f) is not None else None
                except (TypeError, ValueError): v = None
                if _ok(f, v):
                    agg.setdefault(sid, {}).setdefault(f, []).append(v)
        per_sensor = {sid: ({f: round(sum(xs) / len(xs), 1) for f, xs in d.items() if xs},
                            f"{AS_OF}T12:00:00Z") for sid, d in agg.items()}
    else:                                                 # most-recent reading per sensor in the window
        latest = {}
        for rd in rows:
            sid, ts = rd.get("sensorId"), rd.get("timestamp")
            if not sid or not ts or sid not in sensors:
                continue
            if sid not in latest or ts > latest[sid]["timestamp"]:
                latest[sid] = rd
        per_sensor = {}
        for sid, rd in latest.items():
            vals = {}
            for f in _PARAMS:
                try: v = float(rd.get(f)) if rd.get(f) is not None else None
                except (TypeError, ValueError): v = None
                if _ok(f, v):
                    vals[f] = v
            per_sensor[sid] = (vals, _isoz(rd["timestamp"]))

    written = 0
    for sid, (vals, obs) in per_sensor.items():
        if "pm25" not in vals:                             # need a PM2.5 to be useful downstream
            continue
        loc = sensors[sid]
        cell = h3.latlng_to_cell(loc["lat"], loc["lng"], res)
        stn = f"idss-{_slug(sid)}"
        rid = f"ds:{T}:air/sensor_reading/{stn}-{obs[:13]}"
        fact = {"schema": "air.sensor_reading", "id": rid, "sensor_id": stn,
                "space": {"h3": cell, "res": res, "centroid": [round(loc["lng"], 5), round(loc["lat"], 5)]},
                **vals, "observed_at": obs, "source": "idss",
                "prov": {"method": "idss.v1", "component": "ds:airawat:driver/kanpur-idss",
                         "station": loc.get("name") or sid}}
        _call("/v1/write", {"scope": "air.sensor_reading", "id": rid, "object": fact,   # asserted_at = write time (not the reading time)
                            "valid_time": {"from": obs}, "asserted_at": _isoz(datetime.now(timezone.utc))})
        written += 1

    mode = f"AS_OF {AS_OF} (daily mean)" if AS_OF else f"live {WINDOW_H}h (latest/sensor)"
    print(f"ingested {written} REAL air.sensor_reading points from Kanpur iDSS [{mode}] "
          f"into tenant '{T}' @res{res} ({len(sensors)} located sensors, {len(rows)} readings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
