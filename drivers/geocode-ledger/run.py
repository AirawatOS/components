#!/usr/bin/env python3
"""geocode-ledger — turn a property-tax LEDGER (address strings, no coordinates) into geocoded tax.assessment records.

Property-tax ledgers rarely carry lat/long; the address_string IS the geocode source. This driver:
  1. (optional) INGESTS a ledger CSV asset (LEDGER_FILE_ID) → tax.assessment rows (no point yet);
  2. GEOCODES every tax.assessment that has an address_string but no point, via a REAL geocoder (Nominatim by default),
     egress-gated through the broker — writing point + geocode_confidence + address_parse_quality back.
NO fabrication: an address the geocoder cannot resolve is left WITHOUT a point (geocode_confidence 0), counted as
`unresolved`, never given guessed coordinates. Rate-limited + batched (MAX) so a public geocoder is respected.
"""
import os, json, time, csv, io, urllib.parse, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
GEOCODER = os.environ.get("GEOCODER_URL", "https://nominatim.openstreetmap.org/search")
LEDGER_FILE_ID = os.environ.get("LEDGER_FILE_ID", "")
CITY_HINT = os.environ.get("CITY_HINT", "")                 # appended to each query to disambiguate (e.g. "Nashik, India")
MAX = int(os.environ.get("MAX", "50"))                      # cap per run (public geocoders are rate-limited)
SLEEP = float(os.environ.get("GEOCODE_SLEEP", "1.1"))       # ≥1s for Nominatim's usage policy
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"{}")


def unwrap(res):
    if isinstance(res, dict):
        for k in ("body", "data", "result", "response", "content"):
            if k in res:
                return res[k]
    return res


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


def geocode(addr):
    """address_string → (lon, lat, confidence) via the geocoder, or (None, None, 0.0) if UNRESOLVED (never guessed)."""
    q = addr + ((", " + CITY_HINT) if CITY_HINT else "")
    url = GEOCODER + "?" + urllib.parse.urlencode({"q": q, "format": "json", "limit": 1, "addressdetails": 0})
    try:
        hits = _parse_json(unwrap(call("/v1/fetch", {"url": url}))) or []
    except Exception as e:
        print(f"[geocode] fetch error for {addr!r}: {e}", flush=True); return None, None, 0.0
    if not hits:
        return None, None, 0.0
    h = hits[0]
    try:
        return float(h["lon"]), float(h["lat"]), round(float(h.get("importance", 0.5)), 3)
    except Exception:
        return None, None, 0.0


def ingest_csv():
    """Optional: LEDGER_FILE_ID → tax.assessment rows (no point). Expected columns: uid, owner, mobile, address_string,
    ward, assessed_value, annual_tax, paid_status (header row; extra columns ignored)."""
    if not LEDGER_FILE_ID:
        return 0
    raw = unwrap(call("/v1/file/get", {"file_id": LEDGER_FILE_ID}))
    text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    n = 0
    for row in csv.DictReader(io.StringIO(text)):
        uid = (row.get("uid") or row.get("property_id") or "").strip()
        if not uid:
            continue
        aid = f"ds:{TENANT}:tax/assessment/{uid}"
        obj = {"schema": "tax.assessment", "id": aid, "tenant": TENANT, "uid": uid, "owner": row.get("owner"),
               "mobile": row.get("mobile"), "address_string": row.get("address_string") or row.get("address"),
               "ward": row.get("ward"), "paid_status": (row.get("paid_status") or "unknown").strip() or "unknown",
               "source": "geocode-ledger", "created_at": NOW}
        for k, dst in (("assessed_value", "assessed_value"), ("annual_tax", "annual_tax")):
            try: obj[dst] = float(row[k]) if row.get(k) not in (None, "") else None
            except Exception: obj[dst] = None
        call("/v1/write", {"scope": "tax.assessment", "id": aid, "object": {k: v for k, v in obj.items() if v is not None}})
        n += 1
    print(f"[ingest] wrote {n} assessment rows from ledger file", flush=True)
    return n


def main():
    if not TENANT:
        print("[geocode-ledger] no tenant in this context — nothing to do"); return 0
    ingest_csv()
    rows = [r.get("object") or r for r in call("/v1/query", {"scope": "tax.assessment", "limit": 5000}).get("results", [])]
    todo = [a for a in rows if a.get("address_string") and not a.get("point")][:MAX]
    print(f"[geocode-ledger] {len(rows)} assessments, {len(todo)} to geocode (cap {MAX})", flush=True)
    done = unresolved = 0
    for a in todo:
        lon, lat, conf = geocode(a["address_string"])
        upd = {**a, "address_parse_quality": parse_quality(a.get("address_string"))}
        if lon is not None:
            upd["point"] = {"lon": lon, "lat": lat}; upd["geocode_confidence"] = conf; done += 1
        else:
            upd["geocode_confidence"] = 0.0; upd["geocode_band"] = "reject"; unresolved += 1
        call("/v1/write", {"scope": "tax.assessment", "id": a["id"], "object": upd})
        time.sleep(SLEEP)
    print(f"[geocode-ledger] geocoded {done}, unresolved {unresolved} (left without a point — not fabricated)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
