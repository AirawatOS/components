#!/usr/bin/env python3
"""water-setup — seeds the Water Intelligence solution's governed REFERENCE DATA so NO component hardcodes it:
  • water.category    — the water-issue taxonomy (code->label, domain bucket, normalisation keywords)
  • water.record_kind — the contact-type vocabulary (complaint|query|no_response_call; is_complaint flags the denominator)
  • water.config      — the Solution Configuration (agency branding + operational knobs) every member reads

Idempotent: deterministic ids, re-run safe. The shipped values are a sensible DEFAULT taxonomy + a REFERENCE config
(Delhi Jal Board) — an installer edits water.config for their agency and may add/adjust water.category rows as DATA.
Confined — only I/O is the SDK ctx. (Reconstructed 2026-10-23 from the governed build-guide Appendix C.)
"""
import os
from aos import driver, ctx

CATEGORIES = [
    ("tanker_request",   "Water tanker required",       "tanker", ["tanker"], 1),
    ("sewer_overflow",   "Sewer blockage / overflow",   "sewer",  ["sewer", "overflow", "blockage", "drain", "logging", "garbage", "manhole cover"], 2),
    ("manhole",          "Manhole / cover",             "sewer",  ["manhole"], 3),
    ("contamination",    "Dirty / contaminated water",  "water",  ["dirty", "contamination", "quality", "muddy", "smell"], 4),
    ("low_pressure",     "Low pressure",                "water",  ["low pressure", "pressure"], 5),
    ("pipeline_leakage", "Water pipeline leakage",      "water",  ["pipeline", "leakage", "leak", "pipe"], 6),
    ("no_water",         "No water",                    "water",  ["no water", "short supply", "non supply", "dry", "supply"], 7),
    ("road_restoration", "Road restoration",            "water",  ["road", "restoration", "pit"], 8),
    ("billing",          "Billing",                     "water",  ["bill", "billing", "payment"], 9),
    ("new_connection",   "New connection",              "water",  ["new connection", "connection"], 10),
    ("other",            "Other",                       "water",  [], 99),
]
RECORD_KINDS = [
    ("complaint",        "Complaint",         True),
    ("query",            "Query",             False),
    ("no_response_call", "No response call",  False),
]
CONFIG = {
    "agency_name": "Delhi Jal Board",
    "dashboard_title": "Water Intelligence Dashboard",
    "subtitle": "from complaints to insight for better water management",
    "is_live": False,
    "snapshot_caveat": ("This is a historical snapshot, not a live feed. Period-over-period \"trend\" deltas are "
                        "intentionally omitted — the export does not span enough time to compute them honestly "
                        "(the within-window week-over-week is shown instead). Hotspots show association, not causation."),
    "zone_label": "operational zones",
    "attributions": {"complaints_source": "DJB grievance export",
                     "geocoder": "OSM-PBF + PIN + ward",
                     "population_source": "WorldPop 2020 constrained"},
    "severity": {"rate_warn": 150, "rate_crit": 300, "count_crit_pct": 0.10, "count_warn_pct": 0.30},
    "windows": {"window_days": 30, "week_days": 7, "max_weeks": 4, "min_count": 1, "min_chronic": 20},
}


# viz.layer descriptors (operational-load lead) — render water.hotspot on the City Scan map. Shape matches the
# live air-hotspot layer: schema "viz.layer", source_schema the data schema, flat numeric value_field,
# color_scale, render "map", grouped under "Water". The map reads h3_id off each hotspot (added in
# water-hotspots). rate_per_10k is only populated when water-hotspots ran with POP_FILE_ID.
VIZ_LAYERS = [
    {"layer_id": "water-load", "label": "Water complaints — load", "short_label": "Complaint load",
     "order": 40, "value_field": "count", "color_scale": "reds", "higher_is_worse": True, "extrude": True,
     "unit": "complaints",
     "description": "Complaint volume per h3 cell in the trailing window — operational load (where crews are needed).",
     "interpretation": "Taller/redder = more complaints in that cell. Raw volume, not per-capita."},
    {"layer_id": "water-critical", "label": "Critical hotspots", "short_label": "Critical",
     "order": 41, "value_field": "count", "color_scale": "reds", "higher_is_worse": True,
     "filters": {"severity": "critical"}, "unit": "complaints",
     "description": "Only the cells the engine flagged critical (top complaint-count percentile).",
     "interpretation": "The highest-load cells to triage first."},
    {"layer_id": "water-burden", "label": "Burden per 10k residents", "short_label": "Per 10k",
     "order": 42, "value_field": "rate_per_10k", "color_scale": "reds", "higher_is_worse": True,
     "unit": "per 10k",
     "description": "Ward complaint rate normalised by population — per-capita burden (equity view).",
     "interpretation": "Redder = heavier burden per resident; this re-ranks the raw-volume view. Needs water-hotspots run with POP_FILE_ID."},
]


@driver.main
def run():
    T = ctx.tenant
    if not T:
        print("FAIL: no run tenant."); return 1
    reset = os.environ.get("RESET", "0").strip() in ("1", "true", "yes")

    def _have(schema):
        try:
            return {(r.get("object") or r).get("id") for r in ctx.facts(schema).list()}
        except Exception:
            return set()

    cat_rows, rk_rows = [], []
    for code, label, domain, kws, order in CATEGORIES:
        cat_rows.append({"schema": "water.category", "id": "ds:%s:water/category/%s" % (T, code),
                         "code": code, "label": label, "domain": domain, "keywords": kws, "order": order})
    for code, label, is_c in RECORD_KINDS:
        rk_rows.append({"schema": "water.record_kind", "id": "ds:%s:water/record_kind/%s" % (T, code),
                        "code": code, "label": label, "is_complaint": is_c})
    cfg_row = dict(CONFIG, schema="water.config", id="ds:%s:water/config/default" % T)
    viz_rows = [dict(v, schema="viz.layer", id="ds:%s:viz/layer/%s" % (T, v["layer_id"]),
                     source_schema="water.hotspot", render="map", category="natural", group="Water",
                     solution="water-intelligence", enabled=True) for v in VIZ_LAYERS]

    wrote = 0
    if reset:
        wrote = ((ctx.write_many(cat_rows) or 0) + (ctx.write_many(rk_rows) or 0)
                 + (ctx.write_many([cfg_row]) or 0) + (ctx.write_many(viz_rows) or 0))
    else:
        have_cat, have_rk, have_cfg = _have("water.category"), _have("water.record_kind"), _have("water.config")
        have_viz = _have("viz.layer")
        new_cat = [r for r in cat_rows if r["id"] not in have_cat]
        new_rk = [r for r in rk_rows if r["id"] not in have_rk]
        new_cfg = [cfg_row] if cfg_row["id"] not in have_cfg else []
        new_viz = [r for r in viz_rows if r["id"] not in have_viz]
        if new_cat: wrote += ctx.write_many(new_cat) or 0
        if new_rk: wrote += ctx.write_many(new_rk) or 0
        if new_cfg: wrote += ctx.write_many(new_cfg) or 0
        if new_viz: wrote += ctx.write_many(new_viz) or 0

    live_cat = len(_have("water.category")); live_rk = len(_have("water.record_kind")); live_cfg = len(_have("water.config"))
    live_viz = len([1 for i in _have("viz.layer") if ":viz/layer/water-" in i])
    print("water-setup: wrote %d records (RESET=%s). live now: water.category=%d water.record_kind=%d water.config=%d water-viz.layer=%d"
          % (wrote, reset, live_cat, live_rk, live_cfg, live_viz))
    return 0 if (live_cat >= len(CATEGORIES) and live_rk >= len(RECORD_KINDS) and live_cfg >= 1 and live_viz >= len(VIZ_LAYERS)) else 2


if __name__ == "__main__":
    raise SystemExit(driver.run())
