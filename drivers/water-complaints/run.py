#!/usr/bin/env python3
"""water-complaints — load a water-utility GRIEVANCE/COMPLAINTS export into the canonical `water.complaint` schema.

Source-agnostic by design: this ONE driver ingests the complaint feed of ANY water board (DJB, BWSSB, HMWSSB, …).
What differs between boards is CONFIG, never code — the COLUMN MAPPING (auto-detected from the header, env-overridable),
the TAXONOMY CROSSWALK (`TAXONOMY_MAP` JSON: board label → shared canonical category), and the ADMIN HIERARCHY
(`ADMIN_COLUMNS` JSON: level → column). The board's ORIGINAL label is always preserved in `raw_category`.

Privacy: an ANALYTICS ingest — maps ONLY the non-personal analytical fields and DROPS all citizen PII (name, mobile,
email, address). No coordinates are produced here; name-join geocoding is a separate, later step.

The generic plumbing — fetch the blob, parse the CSV, resolve columns, parse dates, derive a STABLE id, and write only
the NEW + CHANGED rows (idempotent re-ingest) — lives in `aos.ingest`. This driver supplies only the water-specific
row mapping + taxonomy; `ingest.table` does the rest.
"""
import os, re, json
from aos import driver, ingest, ctx

# ── OPTIONAL layered geocoder (active only when GAZETTEER_FILE_ID is set) ──────────────────────────────────────────
# Grievance exports carry a LOCALITY/COLONY name + a free-text ADDRESS (with a PIN) + a WARD — not coordinates. To
# place a complaint on the H3 grid WITHOUT storing PII, we resolve the FINEST available name against a local gazetteer
# (OSM localities + PIN centroids + ward centroids, built offline from the city's own OSM PBF + ward boundaries):
#   colony→locality (best) · PIN-in-address→PIN centroid (mid) · ward→ward centroid (floor).
# The address is used TRANSIENTLY for its PIN only; the free-text address is NEVER written (privacy). We persist just
# the derived `centroid` + `h3` + the non-personal `pincode` + the method/confidence (lineage).
_GAZ = {"localities": {}, "pincodes": {}, "wards": {}}
_PIN = re.compile(r'\b(1\d{5})\b')
_CONF = {"colony": 0.8, "pin": 0.5, "ward": 0.3}
_GEO_STATS = {"colony": 0, "pin": 0, "ward": 0, "unplaced": 0}      # coverage tally (printed at end — no silent caps)


def _norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def _load_gazetteer():
    # REGISTRY-FIRST (ADR-0060): the gazetteer-builder writes the gazetteer as governed `space.place` facts, so
    # prefer those (no file to manage). A GAZETTEER_FILE_ID still works as an override / offline fallback.
    gid = os.environ.get("GAZETTEER_FILE_ID", "").strip()
    if not gid:
        try:
            rows = [(r.get("object") or r) for r in ctx.facts("space.place").list()]
        except Exception:
            rows = []
        for o in rows:
            kind, key, c, h = o.get("kind"), o.get("key"), o.get("centroid"), o.get("h3")
            if not (kind and key and c and len(c) >= 2 and h):
                continue
            entry = [c[0], c[1], h]
            if kind == "locality":  _GAZ["localities"][key] = entry
            elif kind == "pincode": _GAZ["pincodes"][key] = entry
            elif kind == "ward":    _GAZ["wards"][key] = entry
        if any((_GAZ["localities"], _GAZ["pincodes"], _GAZ["wards"])):
            print("[water-complaints] gazetteer (registry space.place): %d localities · %d pincodes · %d wards"
                  % (len(_GAZ["localities"]), len(_GAZ["pincodes"]), len(_GAZ["wards"])), flush=True)
            return True
        return False
    raw = ctx.file.get(gid)
    g = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    _GAZ["localities"] = g.get("localities", {}) or {}
    _GAZ["pincodes"] = g.get("pincodes", {}) or {}
    _GAZ["wards"] = g.get("wards", {}) or {}
    print("[water-complaints] gazetteer (file): %d localities · %d pincodes · %d wards"
          % (len(_GAZ["localities"]), len(_GAZ["pincodes"]), len(_GAZ["wards"])), flush=True)
    return True


def _colony_candidates(colony, ward):
    """Grievance colony strings are noisy ('Khanpur Water Emergency- [KHANPUR]', 'New Kondli C1 to C2(Gharoli)').
    Yield normalized name candidates, cleanest first: bracket/paren contents, then the de-noised base, then raw."""
    cands = []
    for m in re.findall(r'[\(\[]([^\)\]]+)[\)\]]', colony or ''):        # [KHANPUR] / (Gharoli)
        cands.append(m)
    base = re.sub(r'[\(\[].*?[\)\]]', '', colony or '')                  # strip brackets
    base = re.sub(r'(?i)\bwater\s*emerg\w*\b', '', base)                 # drop "Water Emergency" noise
    cands += [base, colony or '', ward or '']
    seen, out = set(), []
    for c in cands:
        k = _norm(c)
        if len(k) >= 3 and k not in seen:
            seen.add(k); out.append(k)
    return out


def _geocode(colony, address, ward):
    """Return (centroid[lon,lat], method, confidence, pincode) using the finest available tier, or (None,…)."""
    pin = None
    m = _PIN.search(address or "")
    if m:
        pin = m.group(1)
    for k in _colony_candidates(colony, ward):                          # tier 1: colony/locality
        if k in _GAZ["localities"]:
            return _GAZ["localities"][k], "colony", _CONF["colony"], pin
    if pin and pin in _GAZ["pincodes"]:                                 # tier 2: PIN centroid
        return _GAZ["pincodes"][pin], "pin", _CONF["pin"], pin
    wk = _norm(ward)                                                    # tier 3: ward centroid (floor)
    if wk and wk in _GAZ["wards"]:
        return _GAZ["wards"][wk], "ward", _CONF["ward"], pin
    return None, None, None, pin

# ── canonical taxonomy (must match the governed `water.complaint.category` enum) ───────────────────────────────────
CANONICAL = ["no_water", "low_pressure", "contamination", "tanker_request", "pipeline_leakage",
             "sewer_overflow", "manhole", "road_restoration", "billing", "new_connection", "other"]

# generic keyword classifier — the safety net when TAXONOMY_MAP has no entry. Ordered by specificity (first hit wins);
# water-domain generic, NOT board-specific. A board's explicit TAXONOMY_MAP always takes precedence.
KEYWORDS = [
    ("manhole",          ["manhole"]),
    ("tanker_request",   ["tanker"]),
    ("road_restoration", ["pit", "road construction", "road restoration", "road cutting"]),
    ("pipeline_leakage", ["pipe"]),
    ("sewer_overflow",   ["sewer", "overflow", "blockage", "drain", "logging", "garbage", "manhole cover"]),
    ("contamination",    ["contaminat", "dirty", "quality", "smell", "muddy", "colour", "color"]),
    ("low_pressure",     ["low pressure", "pressure"]),
    ("no_water",         ["no water", "short supply", "supply", "dry", "non supply"]),
    ("new_connection",   ["new connection", "new water connection"]),
    ("billing",          ["bill", "payment", "charge", "arrear", "refund"]),
]

# column auto-detection: env override first (e.g. CATEGORY_FIELD), else the first header matching a candidate.
COLUMNS = {
    "id":       [os.environ.get("ID_FIELD"),          "tracking no", "tracking_no", "complaint id", "complaint_id", "id", "grievance id"],
    "channel":  [os.environ.get("CHANNEL_FIELD"),     "call type", "channel", "source"],
    "category": [os.environ.get("CATEGORY_FIELD"),    "complaint type", "complaint_type", "category", "type"],
    "subcat":   [os.environ.get("SUBCATEGORY_FIELD"), "subcategory", "sub category", "sub-category"],
    "priority": [os.environ.get("PRIORITY_FIELD"),    "priority"],
    "status":   [os.environ.get("STATUS_FIELD"),      "status"],
    "reg_date": [os.environ.get("REGISTERED_FIELD"),  "registered date", "registered_date", "registered on", "created date", "complaint date"],
    "reg_time": [os.environ.get("REGISTERED_TIME_FIELD"), "registered time", "registered_time"],
    "closed":   [os.environ.get("CLOSED_FIELD"),      "closed at", "closed_at", "closed date", "resolved at", "closure date"],
    "reopens":  [os.environ.get("REOPENS_FIELD"),     "number of reopens", "reopens", "reopen count"],
}
# ADMIN_COLUMNS (level→column) declares the board's hierarchy; else auto-detect the common civic levels.
AUTO_ADMIN = {"colony": "colony", "ward": "ward", "ac": "a.c. name", "zone": "zone"}

classify = ingest.crosswalk(canonical=CANONICAL, env="TAXONOMY_MAP", keywords=KEYWORDS)


def map_row(r):
    """One CSV row → a canonical water.complaint envelope (PII dropped), or None to skip. `ingest.table` stamps id +
    tenant and handles idempotent upsert; here we only shape the governed fields."""
    reg = ingest.date(r.get("reg_date"), r.get("reg_time"))
    if not reg:
        return None                                        # registered_at is required by the schema — skip undated rows
    raw = r.get("category") or r.get("subcat")
    admin = r.pick(ingest.jsonenv("ADMIN_COLUMNS") or AUTO_ADMIN)   # {level: value} for non-empty levels
    # RECORD KIND (source disposition, NOT the AI taxonomy): a PGR feed mixes true complaints with queries and
    # no-response calls. Preserve the source's own Complaint/Query/… classification verbatim so analytics can
    # operate on complaints only, while the interactions are retained (not discarded). Column is configurable;
    # defaults to the PGR 'Category' column. Kept SEPARATE from `category` (the AI-normalised water issue type).
    kind_raw = (r.raw(os.environ.get("RECORD_KIND_FIELD", "Category")) or "").strip()
    record_kind = re.sub(r'[^a-z0-9]+', '_', kind_raw.lower()).strip('_') or None  # complaint|query|no_response_call
    rec = {
        "tracking_no":    r.get("id") or None,
        "source_channel": r.get("channel") or None,
        "record_kind":    record_kind,
        "category":       classify(raw),
        "raw_category":   raw or None,
        "subcategory":    r.get("subcat") or None,
        "priority":       r.get("priority") or None,
        "status":         r.get("status") or "unknown",
        "admin_path":     admin or None,
        "ward":           admin.get("ward") or None,
        "zone":           admin.get("zone") or None,
        "registered_at":  reg,
        "closed_at":      ingest.date(r.get("closed")),
        "reopens":        ingest.int_(r.get("reopens")),
    }
    _GEO_STATS["kind_" + (record_kind or "unknown")] = _GEO_STATS.get("kind_" + (record_kind or "unknown"), 0) + 1
    # OPTIONAL geocode — only for COMPLAINTS (queries / no-response calls aren't placed on the grid), and only
    # when a gazetteer is loaded. Address is read ONLY to extract a PIN; it is never stored.
    if record_kind == "complaint" and (_GAZ["localities"] or _GAZ["pincodes"] or _GAZ["wards"]):
        entry, method, conf, pin = _geocode(r.raw("Colony") or admin.get("colony"), r.raw("Address"), rec["ward"])
        if pin:
            rec["pincode"] = pin                                    # non-personal postal area
        # gazetteer entries are [lon, lat, h3cell] — the res-9 cell is PRECOMPUTED at build time (the L1 driver
        # runtime has no h3 lib; the gazetteer builder does), so we just read it. No coordinate math here.
        if entry and len(entry) >= 3 and entry[2]:
            rec["centroid"] = [round(entry[0], 6), round(entry[1], 6)]   # [lon, lat]
            rec["h3"] = entry[2]                                         # res-9 cell (required by water.hotspot)
            rec["geocode_method"] = method
            rec["geocode_confidence"] = conf
            _GEO_STATS[method] += 1
        else:
            _GEO_STATS["unplaced"] += 1
    return rec


@driver.main
def run():
    geocoding = _load_gazetteer()                                  # no-op unless GAZETTEER_FILE_ID is set
    res = ingest.table(
        schema="water.complaint", map_row=map_row, columns=COLUMNS,
        file_env="COMPLAINTS_FILE_ID",
        key_field="tracking_no",                           # board tracking no is the natural key when present…
        key_fields=("raw_category", "registered_at", "source_channel", "ward", "zone"),   # …else hash these STABLE fields
        sig_exclude=("id", "tenant", "source"),            # a change in status/closure/reopen/category ⇒ re-write
        tally_field="category",
        batch=500,                                         # bulk-write via the broker batch endpoint (env INGEST_BATCH overrides)
    )
    kinds = {k[5:]: v for k, v in _GEO_STATS.items() if k.startswith("kind_")}
    print("[water-complaints] record_kind: %s" % kinds, flush=True)
    if geocoding:
        placed = _GEO_STATS["colony"] + _GEO_STATS["pin"] + _GEO_STATS["ward"]
        tot = placed + _GEO_STATS["unplaced"]                   # == complaints seen (only complaints geocode)
        pct = (100.0 * placed / tot) if tot else 0.0
        print("[water-complaints] geocoded %d/%d complaints (%.1f%%) — colony:%d pin:%d ward:%d · unplaced:%d"
              % (placed, tot, pct, _GEO_STATS["colony"], _GEO_STATS["pin"], _GEO_STATS["ward"], _GEO_STATS["unplaced"]),
              flush=True)
        if isinstance(res, dict):
            res["geocode"] = {"placed": placed, "complaints": tot, "coverage_pct": round(pct, 1),
                              "colony": _GEO_STATS["colony"], "pin": _GEO_STATS["pin"], "ward": _GEO_STATS["ward"],
                              "unplaced": _GEO_STATS["unplaced"], "record_kind": kinds}
    return res


if __name__ == "__main__":
    driver.run()
