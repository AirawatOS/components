#!/usr/bin/env python3
"""firms-fire — NASA FIRMS active-fire detections → signal.fire (ADR-0073 M2).

Wind-transported smoke from crop-residue burning in the surrounding states is one of the largest EPISODIC
drivers of Delhi's PM2.5. The fire term — where fires are and how intense (Fire Radiative Power) — is what a
transport-aware model needs (gated by wind direction, an M3 feature). This driver ingests VIIRS/MODIS near-real-
time active-fire points inside the AOI from the NASA FIRMS Area API into signal.fire events.

Migrated to the aos SDK: registry I/O (read space.aoi, write signal.fire), tenant resolution and retry are the
SDK's (ctx.get / ctx.facts / ctx.write_many / ctx.tenant), and the FIRMS egress goes through ctx.fetch — the
SDK's brokered-egress seam — NOT plain urllib, because the FIRMS MAP_KEY lives in the URL PATH and the broker
injects it via `as: template` (the component sends `{KEY}` and never sees the value).

REAL FIRMS only — a fetch error fails loudly. ZERO detections in the AOI/window is a LEGITIMATE result (many
days have no nearby fires, especially outside the Oct–Nov burning season) — reported, exit 0, nothing invented.

  Env: TENANT (SDK resolves via broker context) · AOI_ID (opt) · FIRMS_SOURCE (VIIRS_SNPP_NRT) · DAY_RANGE (1-10, default 2)
       FIRMS_MAP_KEY (Vault, injected by the broker — the component never sees it)
"""
import csv, io, json, os
from aos import driver, ctx

BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{KEY}"
INJECT = {"name": "FIRMS_MAP_KEY", "as": "template", "placeholder": "{KEY}"}
SRC = "ds:airawat:ref/source/firms"


def resolve_aoi():
    aoi_hint = os.environ.get("AOI_ID", "")
    if aoi_hint:
        ht = aoi_hint.split(":")[1] if aoi_hint.count(":") >= 2 else ""
        if ht and ht != ctx.tenant:
            raise SystemExit(f"FATAL: AOI_ID {aoi_hint} belongs to tenant '{ht}', not write tenant '{ctx.tenant}'.")
        o = ctx.get(aoi_hint, "space.aoi")
        if not isinstance(o, dict) or len(o.get("bbox") or []) != 4:
            raise SystemExit(f"FATAL: space.aoi {aoi_hint} missing / no 4-el bbox: {o!r}")
        return [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), aoi_hint
    rows = ctx.facts("space.aoi").list()
    mine = [(r.get("object") or r) for r in rows
            if str((r.get("object") or r).get("id", "")).startswith(f"ds:{ctx.tenant}:")
            and len((r.get("object") or r).get("bbox") or []) == 4]
    if not mine:
        raise SystemExit(f"FATAL: no space.aoi for tenant '{ctx.tenant}' — run City Setup first. No default AOI.")
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


@driver.main
def run():
    if not ctx.tenant:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant.")
    import h3
    firms_source = os.environ.get("FIRMS_SOURCE", "VIIRS_SNPP_NRT")
    day_range = max(1, min(10, int(os.environ.get("DAY_RANGE", "2"))))
    tenant = ctx.tenant
    now = ctx.now()

    bbox, res, aoi_ref = resolve_aoi()
    w, s, e, n = bbox
    url = f"{BASE}/{firms_source}/{w},{s},{e},{n}/{day_range}"
    print(f"tenant={tenant} aoi={aoi_ref} bbox={bbox} · FIRMS {firms_source} last {day_range}d", flush=True)

    r = ctx.fetch(url, inject=INJECT)          # brokered egress: broker injects FIRMS_MAP_KEY into {KEY}, we never see it
    st = r.status
    body = r.body
    if st == 401 or "Invalid" in body[:80] or "MAP_KEY" in body[:120]:
        raise SystemExit("FATAL: NASA FIRMS rejected the key — set a valid FIRMS_MAP_KEY (Connections → Active Fires). Nothing written.")
    if st and st != 200:
        raise SystemExit(f"FATAL: FIRMS returned {st}. Nothing written (no synthetic fallback).")

    rows = list(csv.DictReader(io.StringIO(body)))
    if not rows:
        print(f"no active fires in the AOI over the last {day_range} days — legitimate (nothing written)")
        print(f"RESULT: {json.dumps({'source': 'firms', 'firms_source': firms_source, 'day_range': day_range, 'fires': 0})}")
        return 0

    to_write = []
    for row in rows:
        lat, lon = _num(row.get("latitude")), _num(row.get("longitude"))
        if lat is None or lon is None:
            continue
        occ = _occurred(row.get("acq_date"), row.get("acq_time"))
        if not occ:
            continue
        cell = h3.latlng_to_cell(lat, lon, res)
        sat = row.get("satellite") or firms_source
        oid = f"ds:{tenant}:signal/fire/{sat}-{row.get('acq_date')}-{str(row.get('acq_time')).zfill(4)}-{round(lat,4)}-{round(lon,4)}"
        obj = {"schema": "signal.fire", "id": oid,
               "point": {"lat": round(lat, 5), "lon": round(lon, 5)}, "h3": cell, "resolution": res,
               "frp_mw": round(_num(row.get("frp")), 2) if _num(row.get("frp")) is not None else None,
               "brightness_k": round(_num(row.get("bright_ti4") or row.get("brightness")), 2) if _num(row.get("bright_ti4") or row.get("brightness")) is not None else None,
               "confidence": row.get("confidence"),
               "satellite": sat, "instrument": row.get("instrument"), "daynight": row.get("daynight"),
               "occurred_at": occ, "observed_at": occ,
               "method": f"firms-{firms_source.lower()}", "source_ref": SRC,
               "derived_from": [f"firms:{firms_source}"],
               "prov": {"method": "nasa-firms-area", "source_ref": SRC, "firms_source": firms_source,
                        "derived_from": [f"firms:{firms_source}"], "observed_at": occ}}
        obj = {k: v for k, v in obj.items() if v is not None}
        to_write.append(obj)

    written = ctx.write_many(to_write)

    print(f"ingested {written} REAL signal.fire detections in the AOI ({firms_source}, {day_range}d)", flush=True)
    print(f"RESULT: {json.dumps({'source': 'firms', 'firms_source': firms_source, 'day_range': day_range, 'fires': written})}")
    return 0


if __name__ == "__main__":
    driver.run()
