#!/usr/bin/env python3
"""kanpur-idss driver: Kanpur iDSS low-cost sensor network → air.sensor_reading (points).

Confined — its only I/O is the kernel broker. The iDSS API is district-scoped and LOGIN-authed: the BROKER
performs the login (username/password from Vault) and injects the JWT per this driver's egress auth profile,
so this code never sees the credentials or the token. It fetches sensor metadata + the recent readings
window (paginated), joins them, keeps the latest reading per sensor, and writes air.sensor_reading. It does
NOT fabricate data: if the API is unreachable, auth fails, or there are no located sensors it writes nothing
and says so (non-zero exit on failure).
"""
import json, os, re, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone
import h3

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/kanpur-core")
IDSS = os.environ.get("IDSS_BASE", "http://13.205.88.131:3001")
DISTRICT = os.environ.get("IDSS_DISTRICT", "uttar-pradesh-kanpur")
WINDOW_H = int(os.environ.get("IDSS_WINDOW_HOURS", "6"))
H3_RES = int(os.environ.get("H3_RES", "8"))
WRITE_TENANT = os.environ.get("WRITE_TENANT", "kmc")
_PARAMS = ("pm25", "no2", "o3")                            # air.sensor_reading fields carried through


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=40).read())


def _fetch(url):
    """Brokered GET — the broker logs in + injects the iDSS bearer (auth profile). Returns (status, json|None)."""
    r = _call("/v1/fetch", {"url": url})
    st = r.get("upstream_status")
    if st == 200:
        try:
            return 200, json.loads(r.get("body") or "{}")
        except Exception:
            return 200, None
    return st, None


def _slug(s):
    return re.sub(r"[^a-z0-9_:-]", "-", str(s or "").lower())


def _isoz(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ts


def main():
    aoi = _call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object")
    if not aoi:
        print(f"FAIL: no AOI at {AOI_ID}"); return 1

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

    now = datetime.now(timezone.utc); start = now - timedelta(hours=WINDOW_H)
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    page, rows = 1, []
    while True:
        q = urllib.parse.urlencode({"districtId": DISTRICT, "start_date": fmt(start), "end_date": fmt(now),
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

    latest = {}                                            # sensorId -> most-recent reading in the window
    for rd in rows:
        sid, ts = rd.get("sensorId"), rd.get("timestamp")
        if not sid or not ts or sid not in sensors:
            continue
        if sid not in latest or ts > latest[sid]["timestamp"]:
            latest[sid] = rd

    written = 0
    for sid, rd in latest.items():
        loc = sensors[sid]; obs = _isoz(rd["timestamp"])
        vals = {}
        for f in _PARAMS:
            v = rd.get(f)
            if v is not None:
                try:
                    vals[f] = float(v)
                except (TypeError, ValueError):
                    pass
        if "pm25" not in vals:                             # need a PM2.5 to be useful downstream
            continue
        cell = h3.latlng_to_cell(loc["lat"], loc["lng"], H3_RES)
        stn = f"idss-{_slug(sid)}"
        rid = f"ds:{WRITE_TENANT}:air/sensor_reading/{stn}-{obs[:13]}"
        fact = {"schema": "air.sensor_reading", "id": rid, "sensor_id": stn,
                "space": {"h3": cell, "res": H3_RES, "centroid": [round(loc["lng"], 5), round(loc["lat"], 5)]},
                **vals, "observed_at": obs, "source": "idss",
                "prov": {"method": "idss.v1", "component": "ds:airawat:driver/kanpur-idss",
                         "station": loc.get("name") or sid}}
        _call("/v1/write", {"scope": "air.sensor_reading", "id": rid, "object": fact,   # asserted_at = write time (not the reading time)
                            "valid_time": {"from": obs}, "asserted_at": _isoz(datetime.now(timezone.utc))})
        written += 1

    print(f"ingested {written} REAL air.sensor_reading points from Kanpur iDSS "
          f"({len(sensors)} located sensors, {len(rows)} readings in {WINDOW_H}h)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
