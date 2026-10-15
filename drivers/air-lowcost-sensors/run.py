#!/usr/bin/env python3
"""air-lowcost-sensors driver: an agency iDSS low-cost sensor network → air.sensor_reading (points).

Confined — its only I/O is the kernel broker. The iDSS vendor API is district-scoped and LOGIN-authed: the
BROKER performs the login (username/password from Vault) and injects the JWT per this driver's egress AUTH
PROFILE, so this code never sees the credentials or the token. It fetches sensor metadata + a readings window
(paginated), joins them, and writes air.sensor_reading. Two modes: LIVE (the most recent reading per sensor in
the last IDSS_WINDOW_HOURS) or, when AS_OF_DATE=YYYY-MM-DD is set, the DAILY MEAN per sensor for that historical
day. It writes into whichever tenant RUNS it (broker context) and cells at that tenant's AOI resolution — the
vendor district is configuration (IDSS_DISTRICT), never a hardcoded city. It does NOT fabricate data: if the API
is unreachable, auth fails, or there are no located sensors it writes nothing and says so (non-zero on failure).

aos SDK: registry I/O (tenant resolution, space.aoi reads, air.sensor_reading writes) is the SDK's ctx, and the
iDSS egress goes through ctx.fetch — the SDK's brokered-egress seam — so the broker applies this driver's
declared egress AUTH PROFILE (login → JWT) server-side and the component never holds the credentials/token.
"""
import json, os, re, urllib.parse
from datetime import datetime, timedelta, timezone
from aos import driver, ctx, Reject

CID = "ds:airawatdeveloper:driver/air-lowcost-sensors"
_PARAMS = ("pm25", "no2", "o3")


def _ok(param, v, pm_max):
    """A plausible ambient reading — drops device faults (offline 0, sentinel 999/1000)."""
    if v is None:
        return False
    if param in ("pm25", "pm10") and (v <= 0 or v >= pm_max):
        return False
    return True


def _fetch(url):
    """Brokered GET via ctx.fetch — the broker logs in + injects the vendor bearer (this driver's egress AUTH
    PROFILE), so this code never sees the credentials/token. Returns (status, json|None); a broker/vendor error
    (e.g. 403 when no iDSS credentials are bound) comes back as a code, not a crash."""
    try:
        r = ctx.fetch(url)                       # method defaults GET; auth profile applied broker-side per host
    except Reject as e:
        print(f"[air-lowcost-sensors] brokered egress failed: {e}")
        return 0, None
    if r.status == 200:
        try:
            return 200, r.json()
        except Exception:
            return 200, None
    return r.status, None


def _slug(s):
    return re.sub(r"[^a-z0-9_:-]", "-", str(s or "").lower())


def _isoz(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ts


@driver.main
def run():
    import h3                                    # lazy (heavy dep) — keeps import-time clean for aos check
    AOI_ID = os.environ.get("AOI_ID", "")      # explicit AOI, else discovered for THIS run's tenant — NEVER a hardcoded city
    IDSS = os.environ.get("IDSS_BASE", "http://13.205.88.131:4000")
    DISTRICT = os.environ.get("IDSS_DISTRICT", "").strip()   # the vendor district id — configuration, no hardcoded city
    WINDOW_H = int(os.environ.get("IDSS_WINDOW_HOURS", "6"))
    AS_OF = os.environ.get("AS_OF_DATE", "").strip()          # YYYY-MM-DD: that whole day's REAL readings (daily mean/sensor)
    PM_MAX = float(os.environ.get("PM_MAX_PLAUSIBLE", "985"))  # low-cost sensors emit 0 (offline) / 999,1000 (out-of-range) as
                                                              # faults — reject them (QC, not fabrication) so a dead sensor
                                                              # never becomes a false hotspot. Real extreme days stay <985.
    if not DISTRICT:
        print("FAIL: IDSS_DISTRICT not set — this connector needs the vendor district id (no hardcoded city)."); return 1

    T = ctx.tenant                                            # this run's tenant (broker context) — writes into whoever runs it
    if not T:
        print("FAIL: could not resolve the run tenant (/v1/context)."); return 1
    if AOI_ID:                                              # explicit AOI (must belong to this tenant)
        aoi = ctx.get(AOI_ID, "space.aoi")
    else:                                                  # discover the tenant's largest AOI (no hardcoded city)
        rows = ctx.facts("space.aoi").list()
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
                if _ok(f, v, PM_MAX):
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
                if _ok(f, v, PM_MAX):
                    vals[f] = v
            per_sensor[sid] = (vals, _isoz(rd["timestamp"]))

    facts = []
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
                "prov": {"method": "idss.v1", "component": CID, "station": loc.get("name") or sid}}
        facts.append((fact, obs))
    # bitemporal: each reading's validity is pinned to its own observation time (valid_from), one batched round-trip
    written = ctx.write_many([f for f, _ in facts], valid_from_field="observed_at") if facts else 0

    mode = f"AS_OF {AS_OF} (daily mean)" if AS_OF else f"live {WINDOW_H}h (latest/sensor)"
    print(f"ingested {written} REAL air.sensor_reading points from iDSS district '{DISTRICT}' [{mode}] "
          f"into tenant '{T}' @res{res} ({len(sensors)} located sensors, {len(rows)} readings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(driver.run())
