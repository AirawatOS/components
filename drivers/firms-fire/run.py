#!/usr/bin/env python3
"""firms-fire — NASA FIRMS active-fire detections → signal.fire (ADR-0073 M2).

Wind-transported smoke from crop-residue burning in the surrounding states is one of the largest EPISODIC
drivers of Delhi's PM2.5. The fire term — where fires are and how intense (Fire Radiative Power) — is what a
transport-aware model needs (gated by wind direction, an M3 feature). This driver ingests VIIRS/MODIS near-real-
time active-fire points inside the AOI from the NASA FIRMS Area API into signal.fire events.

Its ONLY I/O is the kernel broker:
  read   the tenant's space.aoi frame (discovered — never a hardcoded default)
  fetch  firms.modaps.eosdis.nasa.gov Area CSV API; the FIRMS MAP_KEY lives in the URL PATH, so the broker
         injects it via `as: template` (the component sends `{KEY}` and never sees the value)
  write  signal.fire — one event per active-fire detection (point + FRP + confidence + acquisition time)

REAL FIRMS only — a fetch error fails loudly. ZERO detections in the AOI/window is a LEGITIMATE result (many
days have no nearby fires, especially outside the Oct–Nov burning season) — reported, exit 0, nothing invented.

  Env: TENANT · AOI_ID (opt) · FIRMS_SOURCE (VIIRS_SNPP_NRT) · DAY_RANGE (1-10, default 2)
       FIRMS_MAP_KEY (Vault, injected by the broker) · RUN_TOKEN · AIRAWAT_API
"""
import csv, io, json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_HINT = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
FIRMS_SOURCE = os.environ.get("FIRMS_SOURCE", "VIIRS_SNPP_NRT")
DAY_RANGE = max(1, min(10, int(os.environ.get("DAY_RANGE", "2"))))
BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{KEY}"
INJECT = {"name": "FIRMS_MAP_KEY", "as": "template", "placeholder": "{KEY}"}
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/firms"
CID = os.environ.get("AIROS_COMPONENT_ID", "ds:airawat:driver/firms-fire")


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def unwrap(res):
    return res.get("object") if isinstance(res, dict) and "object" in res else res


def resolve_aoi():
    if AOI_HINT:
        ht = AOI_HINT.split(":")[1] if AOI_HINT.count(":") >= 2 else ""
        if ht and ht != TENANT:
            raise SystemExit(f"FATAL: AOI_ID {AOI_HINT} belongs to tenant '{ht}', not write tenant '{TENANT}'.")
        o = unwrap(call("/v1/read", {"scope": "space.aoi", "id": AOI_HINT}))
        if not isinstance(o, dict) or len(o.get("bbox") or []) != 4:
            raise SystemExit(f"FATAL: space.aoi {AOI_HINT} missing / no 4-el bbox: {o!r}")
        return [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), AOI_HINT
    rows = call("/v1/query", {"scope": "space.aoi"}).get("results") or []
    mine = [(r.get("object") or r) for r in rows
            if str((r.get("object") or r).get("id", "")).startswith(f"ds:{TENANT}:")
            and len((r.get("object") or r).get("bbox") or []) == 4]
    if not mine:
        raise SystemExit(f"FATAL: no space.aoi for tenant '{TENANT}' — run City Setup first. No default AOI.")
    o = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))
    return [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), o["id"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _occurred(acq_date, acq_time):
    """FIRMS acq_date=YYYY-MM-DD, acq_time=HHMM (UTC) → ISO UTC."""
    t = str(acq_time or "").zfill(4)
    return f"{acq_date}T{t[:2]}:{t[2:4]}:00Z" if acq_date and len(t) == 4 else None


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant.")
    import h3
    bbox, res, aoi_ref = resolve_aoi()
    w, s, e, n = bbox
    url = f"{BASE}/{FIRMS_SOURCE}/{w},{s},{e},{n}/{DAY_RANGE}"
    print(f"tenant={TENANT} aoi={aoi_ref} bbox={bbox} · FIRMS {FIRMS_SOURCE} last {DAY_RANGE}d", flush=True)

    r = call("/v1/fetch", {"url": url, "inject": INJECT})
    st = r.get("upstream_status")
    body = r.get("body") or ""
    if st == 401 or "Invalid" in body[:80] or "MAP_KEY" in body[:120]:
        raise SystemExit("FATAL: NASA FIRMS rejected the key — set a valid FIRMS_MAP_KEY (Connections → Active Fires). Nothing written.")
    if st and st != 200:
        raise SystemExit(f"FATAL: FIRMS returned {st}. Nothing written (no synthetic fallback).")

    rows = list(csv.DictReader(io.StringIO(body)))
    if not rows:
        print(f"no active fires in the AOI over the last {DAY_RANGE} days — legitimate (nothing written)")
        print(f"RESULT: {json.dumps({'source': 'firms', 'firms_source': FIRMS_SOURCE, 'day_range': DAY_RANGE, 'fires': 0})}")
        return 0

    written = 0
    for row in rows:
        lat, lon = _num(row.get("latitude")), _num(row.get("longitude"))
        if lat is None or lon is None:
            continue
        occ = _occurred(row.get("acq_date"), row.get("acq_time"))
        if not occ:
            continue
        cell = h3.latlng_to_cell(lat, lon, res)
        sat = row.get("satellite") or FIRMS_SOURCE
        oid = f"ds:{TENANT}:signal/fire/{sat}-{row.get('acq_date')}-{str(row.get('acq_time')).zfill(4)}-{round(lat,4)}-{round(lon,4)}"
        obj = {"schema": "signal.fire", "id": oid,
               "point": {"lat": round(lat, 5), "lon": round(lon, 5)}, "h3": cell, "resolution": res,
               "frp_mw": round(_num(row.get("frp")), 2) if _num(row.get("frp")) is not None else None,
               "brightness_k": round(_num(row.get("bright_ti4") or row.get("brightness")), 2) if _num(row.get("bright_ti4") or row.get("brightness")) is not None else None,
               "confidence": row.get("confidence"),
               "satellite": sat, "instrument": row.get("instrument"), "daynight": row.get("daynight"),
               "occurred_at": occ, "observed_at": occ,
               "method": f"firms-{FIRMS_SOURCE.lower()}", "source_ref": SRC,
               "derived_from": [f"firms:{FIRMS_SOURCE}"],
               "prov": {"method": "nasa-firms-area", "source_ref": SRC, "firms_source": FIRMS_SOURCE,
                        "derived_from": [f"firms:{FIRMS_SOURCE}"], "observed_at": occ}}
        obj = {k: v for k, v in obj.items() if v is not None}
        call("/v1/write", {"scope": "signal.fire", "id": oid, "object": obj,
                           "valid_time": {"from": occ}, "asserted_at": NOW})
        written += 1

    print(f"ingested {written} REAL signal.fire detections in the AOI ({FIRMS_SOURCE}, {DAY_RANGE}d)", flush=True)
    print(f"RESULT: {json.dumps({'source': 'firms', 'firms_source': FIRMS_SOURCE, 'day_range': DAY_RANGE, 'fires': written})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
