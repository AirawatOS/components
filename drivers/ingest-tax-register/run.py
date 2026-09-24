#!/usr/bin/env python3
"""ingest-tax-register — load a municipal PROPERTY-TAX REGISTER export (the tax roll) into tax.assessment.

The register is the system-of-record from a Municipal Public Service Delivery System (its property-tax module). This
driver is source-agnostic: it reads a CSV asset from the filestore (REGISTER_FILE_ID), maps configurable columns onto
the generic `tax.assessment` schema, and writes them attributed to this component. Design rules:
  - `uid` is the JOIN KEY to the property survey: the survey id where the register carries one, else the register id.
    Rows without a survey key still load — the engine's fuzzy/spatial linkage is the fallback for them.
  - If the register already carries coordinates (many are pre-geocoded), we use them; we NEVER guess a point.
  - Column names are auto-detected from the header (case-insensitive), with optional env overrides — so the same
    driver ingests registers from different systems without code changes. NO fabrication of any field.
"""
import os, json, csv, io, time, base64

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
REGISTER_FILE_ID = os.environ.get("REGISTER_FILE_ID", "")
MAX = int(os.environ.get("MAX", "0"))                       # 0 = no cap (register loads are bounded by the file)
SOURCE = os.environ.get("SOURCE", "property-tax-register")
BATCH = int(os.environ.get("WRITE_BATCH", "1000"))
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

# column auto-detection: env override first, else the first header (case-insensitive) that matches a candidate.
CANDIDATES = {
    "key":      [os.environ.get("KEY_FIELD"),          "surveyid", "survey_id", "uid"],
    "fallback": [os.environ.get("FALLBACK_KEY_FIELD"), "propertyid", "property_id", "oldpropertyid"],
    "lat":      [os.environ.get("LAT_FIELD"),          "predicted_latitude", "latitude", "lat", "y"],
    "lon":      [os.environ.get("LON_FIELD"),          "predicted_longitude", "longitude", "long", "lon", "x"],
    "owner":    [os.environ.get("OWNER_FIELD"),        "ownername", "owner_name", "owner"],
    "mobile":   [os.environ.get("MOBILE_FIELD"),       "mobileno", "mobile_no", "mobile"],
    "address":  [os.environ.get("ADDRESS_FIELD"),      "full_address", "proper_address", "address"],
    "ward":     [os.environ.get("WARD_FIELD"),         "zonename", "ward_no", "ward", "localityname"],
}


def call(path, body):
    import urllib.request
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read() or b"{}")


def read_file(fid):
    """Read a filestore asset via the broker: {id} → {content_b64}. Returns bytes (broker mediates; no direct I/O)."""
    res = call("/v1/file/get", {"id": fid})
    return base64.b64decode(res.get("content_b64") or "")


def _resolve_columns(header):
    """Map each logical field → the actual header name (case-insensitive), honouring env overrides."""
    lower = {h.lower().strip(): h for h in header}
    out = {}
    for field, cands in CANDIDATES.items():
        for c in cands:
            if c and c.lower() in lower:
                out[field] = lower[c.lower()]; break
    return out


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def rows_to_assessments(csv_text, tenant, source=SOURCE):
    """PURE mapping (no I/O): CSV text → tax.assessment envelopes. Yields (id, object). Testable offline."""
    rdr = csv.DictReader(io.StringIO(csv_text))
    cols = _resolve_columns(rdr.fieldnames or [])
    def g(row, field):
        c = cols.get(field)
        return (row.get(c) or "").strip() if c else ""
    for row in rdr:
        uid = g(row, "key") or g(row, "fallback")
        if not uid:
            continue                                        # a register row with no identifier at all is unusable
        aid = f"ds:{tenant}:tax/assessment/{uid}"
        obj = {"schema": "tax.assessment", "id": aid, "tenant": tenant, "uid": uid,
               "owner": g(row, "owner") or None, "mobile": g(row, "mobile") or None,
               "address_string": g(row, "address") or None, "ward": g(row, "ward") or None,
               "paid_status": "unknown",                    # arrears/paid status is out of scope this slice
               "source": source, "created_at": NOW}
        lat, lon = _num(g(row, "lat")), _num(g(row, "lon"))
        if lat is not None and lon is not None:             # register carried coordinates → use them, never guess
            obj["point"] = {"lon": lon, "lat": lat}; obj["geocode_confidence"] = 1.0; obj["geocode_band"] = "provided"
        yield aid, {k: v for k, v in obj.items() if v is not None}


_written = [0]
def _wr(batch):
    r = call("/v1/write/batch", {"items": batch, "asserted_at": NOW}) or {}
    if isinstance(r, dict):
        _written[0] += int(r.get("written", 0) or 0)
        if r.get("errors"):
            print("  batch errors:", json.dumps(r["errors"][:3])[:400], flush=True)
        elif r.get("written") is None:
            print("  batch resp (no 'written' key):", json.dumps(r)[:300], flush=True)


def main():
    if not TENANT:
        print("[ingest-tax-register] no tenant in this context — nothing to do"); return 0
    if not REGISTER_FILE_ID:
        print("[ingest-tax-register] REGISTER_FILE_ID not set — nothing to ingest"); return 0
    text = read_file(REGISTER_FILE_ID).decode("utf-8", "replace")
    batch, n, geoc = [], 0, 0
    for aid, obj in rows_to_assessments(text, TENANT):
        batch.append({"scope": "tax.assessment", "id": aid, "object": obj})   # {items:[{scope,id,object}], asserted_at}
        n += 1; geoc += 1 if obj.get("point") else 0
        if len(batch) >= BATCH:
            _wr(batch); batch = []
        if MAX and n >= MAX:
            break
    if batch:
        _wr(batch)
    print(f"[ingest-tax-register] processed {n} rows ({geoc} with coordinates); PERSISTED {_written[0]} tax.assessment", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
