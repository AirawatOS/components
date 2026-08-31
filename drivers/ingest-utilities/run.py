#!/usr/bin/env python3
"""ingest-utilities — load utility connections (electricity / water) as util.connection, a proxy that a property is
occupied/built. If a row already carries lat/long we use it DIRECTLY; otherwise we geocode the address_string (real
geocoder, egress-gated) — same fail-loud rule as the ledger (an unresolved address gets no point, never a guess).

  1. (optional) INGEST a utility CSV asset (UTIL_FILE_ID) — columns: uid, utility_type, account_no, consumer_name,
     mobile, address_string, lat, lon, tier;
  2. GEOCODE any util.connection with an address_string but no point.
"""
import os, json, time, csv, io, urllib.parse, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
GEOCODER = os.environ.get("GEOCODER_URL", "https://nominatim.openstreetmap.org/search")
UTIL_FILE_ID = os.environ.get("UTIL_FILE_ID", "")
CITY_HINT = os.environ.get("CITY_HINT", "")
MAX = int(os.environ.get("MAX", "50"))
SLEEP = float(os.environ.get("GEOCODE_SLEEP", "1.1"))
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
TYPES = {"electricity", "water", "sewer", "gas"}


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


def _json(v):
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return None


def geocode(addr):
    q = addr + ((", " + CITY_HINT) if CITY_HINT else "")
    url = GEOCODER + "?" + urllib.parse.urlencode({"q": q, "format": "json", "limit": 1})
    try:
        hits = _json(unwrap(call("/v1/fetch", {"url": url}))) or []
    except Exception as e:
        print(f"[geocode] error {addr!r}: {e}", flush=True); return None, None, 0.0
    if not hits:
        return None, None, 0.0
    try:
        return float(hits[0]["lon"]), float(hits[0]["lat"]), round(float(hits[0].get("importance", 0.5)), 3)
    except Exception:
        return None, None, 0.0


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except Exception:
        return None


def ingest_csv():
    if not UTIL_FILE_ID:
        return 0
    raw = unwrap(call("/v1/file/get", {"file_id": UTIL_FILE_ID}))
    text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    n = 0
    for row in csv.DictReader(io.StringIO(text)):
        uid = (row.get("uid") or row.get("account_no") or "").strip()
        utype = (row.get("utility_type") or "electricity").strip().lower()
        if not uid or utype not in TYPES:
            continue
        cid = f"ds:{TENANT}:util/connection/{utype}-{uid}"
        lon, lat = _num(row.get("lon") or row.get("long") or row.get("longitude")), _num(row.get("lat") or row.get("latitude"))
        obj = {"schema": "util.connection", "id": cid, "tenant": TENANT, "uid": uid, "utility_type": utype,
               "account_no": row.get("account_no"), "consumer_name": row.get("consumer_name"), "mobile": row.get("mobile"),
               "address_string": row.get("address_string") or row.get("address"), "tier": row.get("tier"),
               "source": "ingest-utilities", "created_at": NOW}
        if lon is not None and lat is not None:                # lat/long present → use DIRECTLY (no geocode needed)
            obj["point"] = {"lon": lon, "lat": lat}; obj["geocode_confidence"] = 1.0; obj["geocode_band"] = "high"
        call("/v1/write", {"scope": "util.connection", "id": cid, "object": {k: v for k, v in obj.items() if v is not None}})
        n += 1
    print(f"[ingest] wrote {n} utility connections from file", flush=True)
    return n


def main():
    if not TENANT:
        print("[ingest-utilities] no tenant in this context — nothing to do"); return 0
    ingest_csv()
    rows = [r.get("object") or r for r in call("/v1/query", {"scope": "util.connection", "limit": 5000}).get("results", [])]
    todo = [u for u in rows if u.get("address_string") and not u.get("point")][:MAX]
    print(f"[ingest-utilities] {len(rows)} connections, {len(todo)} to geocode (cap {MAX})", flush=True)
    done = unresolved = 0
    for u in todo:
        lon, lat, conf = geocode(u["address_string"])
        upd = dict(u)
        if lon is not None:
            upd["point"] = {"lon": lon, "lat": lat}; upd["geocode_confidence"] = conf; done += 1
        else:
            upd["geocode_confidence"] = 0.0; upd["geocode_band"] = "reject"; unresolved += 1
        call("/v1/write", {"scope": "util.connection", "id": u["id"], "object": upd})
        time.sleep(SLEEP)
    print(f"[ingest-utilities] geocoded {done}, unresolved {unresolved} (no fabricated points)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
