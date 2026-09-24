#!/usr/bin/env python3
"""ingest-utilities — load utility connections (electricity / water) as util.connection, a proxy that a property is
occupied/built. If a row already carries lat/long we use it DIRECTLY; otherwise we geocode the address_string (real
geocoder, egress-gated) — same fail-loud rule as the ledger (an unresolved address gets no point, never a guess).

  1. (optional) INGEST a utility CSV asset (UTIL_FILE_ID) — columns: uid, utility_type, account_no, consumer_name,
     mobile, address_string, lat, lon, tier;
  2. GEOCODE any util.connection with an address_string but no point.
"""
import os, json, time, csv, io, base64, urllib.parse, urllib.request

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
    res = call("/v1/file/get", {"id": UTIL_FILE_ID})              # broker: {id} -> {content_b64}
    text = base64.b64decode(res.get("content_b64") or "").decode("utf-8", "replace")
    batch = []
    for row in csv.DictReader(io.StringIO(text)):
        low = {k.lower().strip(): v for k, v in row.items()}
        uid = (low.get("uid") or low.get("account_no") or "").strip()
        utype = (low.get("utility_type") or "electricity").strip().lower()
        if not uid or utype not in TYPES:
            continue
        cid = f"ds:{TENANT}:util/connection/{utype}-{uid}"
        lon, lat = _num(low.get("lon") or low.get("long") or low.get("longitude")), _num(low.get("lat") or low.get("latitude"))
        obj = {"schema": "util.connection", "id": cid, "tenant": TENANT, "uid": uid, "utility_type": utype,
               "account_no": low.get("account_no"), "consumer_name": low.get("consumer_name"), "mobile": low.get("mobile"),
               "address_string": low.get("address_string") or low.get("address"), "tier": low.get("tier"),
               # parcel_id links the connection DIRECTLY to a cadastral parcel (the survey key) when the utility list
               # carries one — so the engine needn't geocode + spatial-join to place it. Optional; auto-detected.
               "parcel_id": (low.get("parcel_id") or low.get("property_id") or low.get("survey_id") or None),
               "source": "ingest-utilities", "created_at": NOW}
        if lon is not None and lat is not None:                # lat/long present → use DIRECTLY (no geocode needed)
            obj["point"] = {"lon": lon, "lat": lat}; obj["geocode_confidence"] = 1.0; obj["geocode_band"] = "high"
        batch.append({"scope": "util.connection", "id": cid, "object": {k: v for k, v in obj.items() if v is not None}})
    written = 0
    for i in range(0, len(batch), 1000):
        r = call("/v1/write/batch", {"items": batch[i:i + 1000], "asserted_at": NOW}) or {}
        written += int(r.get("written", 0) or 0)
        if r.get("errors"):
            print("  batch errors:", json.dumps(r["errors"][:3])[:400], flush=True)
    print(f"[ingest] PERSISTED {written}/{len(batch)} utility connections from file", flush=True)
    return written


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
