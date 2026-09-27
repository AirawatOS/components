#!/usr/bin/env python3
"""geocode-ledger — turn a property-tax LEDGER (address strings, no coordinates) into geocoded tax.assessment records.

Property-tax ledgers rarely carry lat/long; the address_string IS the geocode source. This driver:
  1. (optional) INGESTS a ledger CSV asset (LEDGER_FILE_ID) → tax.assessment rows (no point yet);
  2. GEOCODES every tax.assessment that has an address_string but no point, via a REAL geocoder (Nominatim by default),
     writing point + geocode_confidence + address_parse_quality back.
NO fabrication: an address the geocoder cannot resolve is left WITHOUT a point (geocode_confidence 0), counted as
`unresolved`, never given guessed coordinates. Rate-limited + batched (MAX) so a public geocoder is respected.

SDK migration (aos): the broker transport, tenant resolution, retry and reads/writes tracking are the SDK's — the
registry I/O is `ctx.facts` / `ctx.write`, and the optional ledger-CSV ingest reads its blob through `ctx.file.get`.
The geocoder call is EXTERNAL EGRESS and stays plain urllib (it no longer routes through the broker /v1/fetch proxy).
"""
import os, json, time, csv, io, urllib.parse, urllib.request
from aos import driver, ctx, egress

# egress user-agent — Nominatim's usage policy requires one; previously supplied by the broker egress config (meta.yaml).
USER_AGENT = "AirawatOS/geocode-ledger (+https://airawat.org; manishsv@gmail.com)"

# Declared egress → aos derives meta.egress/sources from THIS (no hand-maintained YAML). The Nominatim call is
# public/keyless (plain urllib per policy), but the declaration still records the source for attribution + rate.
egress("nominatim.openstreetmap.org", rate_per_min=30, user_agent=USER_AGENT,
       provides=["tax.assessment"], source_ref="ds:airawat:ref/source/nominatim")


def _parse_json(v):
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return None


def parse_quality(addr):
    n = len([t for t in (addr or "").replace(",", " ").split() if t])
    return "high" if n >= 5 else "medium" if n >= 3 else "low" if n >= 1 else "none"


def geocode(addr, geocoder, city_hint):
    """address_string → (lon, lat, confidence) via the geocoder, or (None, None, 0.0) if UNRESOLVED (never guessed)."""
    q = addr + ((", " + city_hint) if city_hint else "")
    url = geocoder + "?" + urllib.parse.urlencode({"q": q, "format": "json", "limit": 1, "addressdetails": 0})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})   # EXTERNAL EGRESS — plain urllib
        with urllib.request.urlopen(req, timeout=120) as r:
            hits = _parse_json(r.read() or b"[]") or []
    except Exception as e:
        print(f"[geocode] fetch error for {addr!r}: {e}", flush=True); return None, None, 0.0
    if not hits:
        return None, None, 0.0
    h = hits[0]
    try:
        return float(h["lon"]), float(h["lat"]), round(float(h.get("importance", 0.5)), 3)
    except Exception:
        return None, None, 0.0


def ingest_csv(tenant, ledger_file_id, now):
    """Optional: LEDGER_FILE_ID → tax.assessment rows (no point). Expected columns: uid, owner, mobile, address_string,
    ward, assessed_value, annual_tax, paid_status (header row; extra columns ignored)."""
    if not ledger_file_id:
        return 0
    raw = ctx.file.get(ledger_file_id)                     # content-addressed blob → raw bytes (SDK filestore seam)
    text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    n = 0
    for row in csv.DictReader(io.StringIO(text)):
        uid = (row.get("uid") or row.get("property_id") or "").strip()
        if not uid:
            continue
        aid = f"ds:{tenant}:tax/assessment/{uid}"
        obj = {"schema": "tax.assessment", "id": aid, "tenant": tenant, "uid": uid, "owner": row.get("owner"),
               "mobile": row.get("mobile"), "address_string": row.get("address_string") or row.get("address"),
               "ward": row.get("ward"), "paid_status": (row.get("paid_status") or "unknown").strip() or "unknown",
               "source": "geocode-ledger", "created_at": now}
        for k, dst in (("assessed_value", "assessed_value"), ("annual_tax", "annual_tax")):
            try: obj[dst] = float(row[k]) if row.get(k) not in (None, "") else None
            except Exception: obj[dst] = None
        ctx.write({k: v for k, v in obj.items() if v is not None})
        n += 1
    print(f"[ingest] wrote {n} assessment rows from ledger file", flush=True)
    return n


@driver.main
def run():
    tenant = ctx.tenant                                    # resolved from the broker context by the SDK
    geocoder = os.environ.get("GEOCODER_URL", "https://nominatim.openstreetmap.org/search")
    ledger_file_id = os.environ.get("LEDGER_FILE_ID", "")
    city_hint = os.environ.get("CITY_HINT", "")            # appended to each query to disambiguate (e.g. "Nashik, India")
    max_n = int(os.environ.get("MAX", "50"))               # cap per run (public geocoders are rate-limited)
    sleep = float(os.environ.get("GEOCODE_SLEEP", "1.1"))  # ≥1s for Nominatim's usage policy
    now = ctx.now()

    if not tenant:
        print("[geocode-ledger] no tenant in this context — nothing to do"); return 0
    ingest_csv(tenant, ledger_file_id, now)
    rows = [r.get("object") or r for r in ctx.facts("tax.assessment").list()]
    todo = [a for a in rows if a.get("address_string") and not a.get("point")][:max_n]
    print(f"[geocode-ledger] {len(rows)} assessments, {len(todo)} to geocode (cap {max_n})", flush=True)
    done = unresolved = 0
    for a in todo:
        lon, lat, conf = geocode(a["address_string"], geocoder, city_hint)
        upd = {**a, "address_parse_quality": parse_quality(a.get("address_string"))}
        if lon is not None:
            upd["point"] = {"lon": lon, "lat": lat}; upd["geocode_confidence"] = conf; done += 1
        else:
            upd["geocode_confidence"] = 0.0; upd["geocode_band"] = "reject"; unresolved += 1
        ctx.write(upd)
        time.sleep(sleep)
    print(f"[geocode-ledger] geocoded {done}, unresolved {unresolved} (left without a point — not fabricated)", flush=True)
    return 0


if __name__ == "__main__":
    driver.run()
