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
import os
from aos import driver, ingest

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
    return {
        "tracking_no":    r.get("id") or None,
        "source_channel": r.get("channel") or None,
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


@driver.main
def run():
    return ingest.table(
        schema="water.complaint", map_row=map_row, columns=COLUMNS,
        file_env="COMPLAINTS_FILE_ID",
        key_field="tracking_no",                           # board tracking no is the natural key when present…
        key_fields=("raw_category", "registered_at", "source_channel", "ward", "zone"),   # …else hash these STABLE fields
        sig_exclude=("id", "tenant", "source"),            # a change in status/closure/reopen/category ⇒ re-write
        tally_field="category",
    )


if __name__ == "__main__":
    driver.run()
