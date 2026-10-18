#!/usr/bin/env python3
"""water-complaints — load a water-utility GRIEVANCE/COMPLAINTS export into the canonical `water.complaint` schema.

Source-agnostic by design: this ONE driver ingests the complaint feed of ANY water board (DJB, BWSSB, HMWSSB, …).
What differs between boards is CONFIG, never code:
  - COLUMN MAPPING — which export columns carry the id / channel / category / status / dates / admin levels. Auto-
    detected from the header (case-insensitive) with optional env overrides, so a new board usually needs no config.
  - TAXONOMY CROSSWALK — each board labels its complaint types differently ("Dirty water (Contamination)",
    "No water", …). `TAXONOMY_MAP` (JSON: raw-label-lowercased → canonical category) maps a board's vocabulary onto
    the SHARED canonical taxonomy. Labels the map does not cover fall to a generic keyword classifier, and anything
    still unresolved is `other` — but the board's ORIGINAL label is ALWAYS preserved in `raw_category` (never lost).
  - ADMIN HIERARCHY — `ADMIN_COLUMNS` (JSON: level-name → column) declares the board's own hierarchy (colony → ward →
    zone, or sub-division → circle, …). Stored as a generic `admin_path` object plus flat `ward`/`zone` for grouping.

Privacy: a complaint record carries citizen PII (name, mobile, email, address). This is an ANALYTICS ingest — it maps
ONLY the non-personal analytical fields (category, status, admin location, timestamps, reopens) and DROPS all PII. No
coordinates are produced here; name-join geocoding (ward/zone → boundary centroid → H3) is a separate, later step.

Confined (aos SDK): the only I/O is the broker — the CSV blob via `ctx.file.get`, the writes via `ctx.write`.
NO fabrication: a field absent from the row is left unset; an unparseable date is dropped, never guessed.
"""
import os, json, csv, io, re, hashlib
from aos import driver, ctx

# ── canonical taxonomy (must match the governed `water.complaint.category` enum) ───────────────────────────────────
CANONICAL = ["no_water", "low_pressure", "contamination", "tanker_request", "pipeline_leakage",
             "sewer_overflow", "manhole", "road_restoration", "billing", "new_connection", "other"]

# generic keyword classifier — the safety net when TAXONOMY_MAP has no entry for a raw label. Ordered by specificity
# (first hit wins). Water-domain generic, NOT board-specific; a board's explicit TAXONOMY_MAP always takes precedence.
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

# column auto-detection: env override first, else the first header (case-insensitive) matching a candidate.
CANDIDATES = {
    "id":        [os.environ.get("ID_FIELD"),          "tracking no", "tracking_no", "complaint id", "complaint_id", "id", "grievance id"],
    "channel":   [os.environ.get("CHANNEL_FIELD"),     "call type", "channel", "source"],
    "category":  [os.environ.get("CATEGORY_FIELD"),    "complaint type", "complaint_type", "category", "type"],
    "subcat":    [os.environ.get("SUBCATEGORY_FIELD"), "subcategory", "sub category", "sub-category"],
    "priority":  [os.environ.get("PRIORITY_FIELD"),    "priority"],
    "status":    [os.environ.get("STATUS_FIELD"),      "status"],
    "reg_date":  [os.environ.get("REGISTERED_FIELD"),  "registered date", "registered_date", "registered on", "created date", "complaint date"],
    "reg_time":  [os.environ.get("REGISTERED_TIME_FIELD"), "registered time", "registered_time"],
    "closed":    [os.environ.get("CLOSED_FIELD"),      "closed at", "closed_at", "closed date", "resolved at", "closure date"],
    "reopens":   [os.environ.get("REOPENS_FIELD"),     "number of reopens", "reopens", "reopen count"],
}


def _resolve_columns(header):
    """Map each logical field → the actual header name (case-insensitive), honouring env overrides."""
    lower = {(h or "").lower().strip(): h for h in header}
    out = {}
    for field, cands in CANDIDATES.items():
        for c in cands:
            if c and c.lower() in lower:
                out[field] = lower[c.lower()]; break
    return out


def _admin_columns(header):
    """ADMIN_COLUMNS (JSON level→column) declares the board's hierarchy; else auto-detect the common civic levels."""
    cfg = os.environ.get("ADMIN_COLUMNS")
    if cfg:
        try:
            return {k: v for k, v in json.loads(cfg).items() if v}
        except Exception:
            print("[water-complaints] ADMIN_COLUMNS is not valid JSON — ignoring, auto-detecting", flush=True)
    lower = {(h or "").lower().strip(): h for h in header}
    auto = {"colony": ["colony", "locality"], "ward": ["ward"], "ac": ["a.c. name", "ac name", "assembly"],
            "zone": ["zone", "division", "circle"]}
    out = {}
    for level, cands in auto.items():
        for c in cands:
            if c in lower:
                out[level] = lower[c]; break
    return out


def _taxonomy_map():
    """TAXONOMY_MAP (JSON: raw-label-lowercased → canonical). Invalid/absent → empty (keyword classifier takes over)."""
    cfg = os.environ.get("TAXONOMY_MAP")
    if not cfg:
        return {}
    try:
        m = {str(k).lower().strip(): str(v).strip() for k, v in json.loads(cfg).items()}
        bad = {v for v in m.values() if v not in CANONICAL}
        if bad:
            print(f"[water-complaints] TAXONOMY_MAP targets not in canonical taxonomy (ignored): {sorted(bad)}", flush=True)
        return {k: v for k, v in m.items() if v in CANONICAL}
    except Exception:
        print("[water-complaints] TAXONOMY_MAP is not valid JSON — ignoring, using keyword classifier", flush=True)
        return {}


def classify(raw_label, tmap):
    """raw complaint-type label → canonical category. Explicit map first, then keyword classifier, then 'other'."""
    s = (raw_label or "").lower().strip()
    if not s:
        return "other"
    if s in tmap:
        return tmap[s]
    for canon, kws in KEYWORDS:
        if any(k in s for k in kws):
            return canon
    return "other"


_DATE = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]m)?)?", re.I)

def to_iso(date_s, time_s=""):
    """Tolerant DD/MM/YYYY[ HH:MM:SS am/pm] → ISO8601 UTC-naive 'YYYY-MM-DDTHH:MM:SS'. Unparseable → None (never guessed).
    Also accepts an already-ISO value. `time_s` (a separate time column) fills the clock when the date field has none."""
    s = (date_s or "").strip()
    if not s:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):                 # already ISO-ish
        return s.replace(" ", "T")[:19]
    m = _DATE.match(s)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    hh, mm, ss, ap = m.group(4), m.group(5), m.group(6), (m.group(7) or "").lower()
    if hh is None and time_s:                              # pull clock from a separate time column
        tm = re.match(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]m)?", time_s.strip(), re.I)
        if tm:
            hh, mm, ss, ap = tm.group(1), tm.group(2), tm.group(3), (tm.group(4) or "").lower()
    hh = int(hh) if hh is not None else 0
    mm = int(mm) if mm is not None else 0
    ss = int(ss) if ss is not None else 0
    if ap == "pm" and hh < 12:
        hh += 12
    elif ap == "am" and hh == 12:
        hh = 0
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}T{hh:02d}:{mm:02d}:{ss:02d}"


def rows_to_complaints(csv_text, tenant, source=None):
    """PURE mapping (no I/O): CSV text → canonical water.complaint envelopes. Yields (id, object). Testable offline."""
    source = source or os.environ.get("SOURCE", "water-grievance")
    rdr = csv.DictReader(io.StringIO(csv_text))
    cols = _resolve_columns(rdr.fieldnames or [])
    admin = _admin_columns(rdr.fieldnames or [])
    tmap = _taxonomy_map()
    seq = 0

    def g(row, field):
        c = cols.get(field)
        return (row.get(c) or "").strip() if c else ""

    for row in rdr:
        seq += 1
        raw_cat = g(row, "category") or g(row, "subcat")
        category = classify(raw_cat, tmap)
        reg = to_iso(g(row, "reg_date"), g(row, "reg_time"))
        if not reg:
            continue                                       # registered_at is required by the schema — skip undated rows
        admin_path = {lvl: (row.get(c) or "").strip() for lvl, c in admin.items() if (row.get(c) or "").strip()}
        tracking = g(row, "id")
        if tracking:
            key = re.sub(r"[^A-Za-z0-9_.-]", "_", tracking)
        else:
            # No board tracking number → derive a STABLE key from the row's stable content so the SAME complaint in
            # a later (overlapping) file collapses onto the same id instead of duplicating. Never a volatile row index.
            sig = "|".join([raw_cat, reg, g(row, "channel")] + [str(v) for v in admin_path.values()])
            key = "h" + hashlib.sha1(sig.encode("utf-8")).hexdigest()[:20]
        cid = f"ds:{tenant}:water/complaint/{key}"
        reopens = None
        rv = g(row, "reopens")
        if rv:
            try:
                reopens = int(float(rv))
            except Exception:
                reopens = None
        obj = {"schema": "water.complaint", "id": cid, "tenant": tenant,
               "tracking_no": tracking,
               "source_channel": g(row, "channel") or None,
               "category": category,
               "raw_category": raw_cat or None,
               "subcategory": g(row, "subcat") or None,
               "priority": (g(row, "priority") or None),
               "status": (g(row, "status") or "unknown"),
               "admin_path": admin_path or None,
               "ward": admin_path.get("ward") or None,
               "zone": admin_path.get("zone") or None,
               "registered_at": reg,
               "closed_at": to_iso(g(row, "closed")),
               "reopens": reopens,
               "source": source}
        yield cid, {k: v for k, v in obj.items() if v is not None}


# fields whose change means the complaint genuinely CHANGED and must be re-written (status moving, a closure, a
# reopen, a reclassification). Everything else is static per complaint, so an identical row is a no-op we skip.
_SIG_FIELDS = ("category", "raw_category", "subcategory", "priority", "status", "closed_at", "reopens",
               "ward", "zone", "source_channel")

def _sig(obj):
    return json.dumps({k: obj.get(k) for k in _SIG_FIELDS}, sort_keys=True, ensure_ascii=False)


@driver.main
def run():
    tenant = ctx.tenant
    if not tenant:
        print("[water-complaints] no tenant in this context — nothing to do"); return 0
    file_id = os.environ.get("COMPLAINTS_FILE_ID", "")
    if not file_id:
        print("[water-complaints] COMPLAINTS_FILE_ID not set — nothing to ingest"); return 0
    max_n = int(os.environ.get("MAX", "0"))                # 0 = no cap
    skip_unchanged = os.environ.get("SKIP_UNCHANGED", "1") not in ("0", "false", "False")

    # Idempotent ingest: load the signatures of complaints already stored (keyed by their deterministic id) ONCE, so a
    # re-sent or overlapping file re-writes only the NEW complaints and the ones whose status/closure/reopens changed.
    existing = {}
    if skip_unchanged:
        try:
            for r in ctx.facts("water.complaint").list():
                o = r.get("object") or r
                if o.get("id"):
                    existing[o["id"]] = _sig(o)
        except Exception as e:
            print(f"[water-complaints] could not preload existing (treating all as new): {e}", flush=True)

    raw = ctx.file.get(file_id)
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    new = changed = unchanged = 0
    tally = {}
    for cid, obj in rows_to_complaints(text, tenant):
        if skip_unchanged and cid in existing:
            if existing[cid] == _sig(obj):
                unchanged += 1
                continue                                   # same complaint, nothing changed → do not re-write
            changed += 1
        else:
            new += 1
        ctx.write(obj)
        tally[obj["category"]] = tally.get(obj["category"], 0) + 1
        if max_n and (new + changed) >= max_n:
            break
    top = sorted(tally.items(), key=lambda kv: -kv[1])[:6]
    print(f"[water-complaints] wrote {new} new + {changed} changed water.complaint; skipped {unchanged} unchanged "
          f"(dedup by stable id); categories touched: {top}", flush=True)
    return 0


if __name__ == "__main__":
    driver.run()
