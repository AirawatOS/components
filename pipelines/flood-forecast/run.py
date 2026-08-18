#!/usr/bin/env python3
"""Flood-forecast pipeline — the FRONT HALF of the decision loop: sense -> predict -> detect -> advise.

Four stages, each the job of a distinct component (here run in one process; production = separate
certified drivers/engines through the broker):

  1 SENSE   (weather driver)      -> met.forecast          rainfall forecast for the AOI
  2 PREDICT (flood-risk engine)   -> risk.flood per cell   model: terrain(elev,slope) x forecast -> 0..1
  3 DETECT  (threshold rule)      -> signal.alert          risk.flood.score >= T over N cells fired
  4 ADVISE  (guideline engine)    -> governance.recommendation[]   from the KNOWLEDGE BASE, each CITING
                                     + platform.case + platform.task(decide) + inbox item   a guideline

The output is exactly the decision the Inbox/decision loop already consumes — but now DERIVED, not
hand-seeded. The AI produces facts, a prediction, and cited recommendations; the human still decides.

Writes go to the kernel registry directly (like the notify/decision reference services). In production
these are certified components writing through the broker with steward grants + attributed provenance.
"""
import argparse, json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
NOTIFY = os.environ.get("NOTIFY_URL", "http://localhost:8094")
TENANT = os.environ.get("TENANT", "kmc")
AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/kanpur-core")
RAINFALL_MM = float(os.environ.get("RAINFALL_MM", "125"))
DECIDER = os.environ.get("DECIDER", "user://kmc/kanpur-admin")
THRESHOLD = 0.70
LEVEL_RANK = {"low": 0, "moderate": 1, "high": 2, "severe": 3}


def put(o):
    r = urllib.request.Request(API + "/v1/registry/objects",
        data=json.dumps({"id": o["id"], "object": o}).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(r, timeout=10).read()

def resolve(flt):
    r = urllib.request.Request(API + "/v1/registry/resolve",
        data=json.dumps({"filter": flt, "limit": 5000}).encode(), headers={"Content-Type": "application/json"})
    return [x["object"] for x in json.loads(urllib.request.urlopen(r, timeout=20).read()).get("results", [])]

def notify(p):
    try:
        r = urllib.request.Request(NOTIFY + "/notify", data=json.dumps(p).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(r, timeout=8).read()
    except Exception as e:
        print("  (notify failed:", e, ")")

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---- knowledge base + presentation (idempotent reference data) -------------------------------------
GUIDELINES = [
    {"id": f"ds:{TENANT}:knowledge/guideline/flood-response", "title": "Activate multi-agency flood response (SOP)",
     "trigger_level": "high", "proposed_action": "Activate the flood-response SOP across Sanitation, Water and Disaster Mgmt",
     "citation": "KMC IRS Activation SOP §2", "confidence": 0.9, "sop_workflow": f"ds:{TENANT}:workflow/flood-response"},
    {"id": f"ds:{TENANT}:knowledge/guideline/flood-pumping", "title": "Pre-position pumps at ponding hotspots",
     "trigger_level": "high", "proposed_action": "Deploy dewatering pumps to the highest-risk low-lying cells",
     "citation": "KMC Monsoon SOP §4.2", "confidence": 0.85, "sop_workflow": f"ds:{TENANT}:workflow/pump-deployment"},
    {"id": f"ds:{TENANT}:knowledge/guideline/flood-desilt", "title": "Emergency desilting of primary drains",
     "trigger_level": "moderate", "proposed_action": "Desilt primary drains in the affected catchment",
     "citation": "KMC Drainage SOP §3.1", "confidence": 0.8, "sop_workflow": f"ds:{TENANT}:workflow/drain-desilting"},
]
# SOP WORKFLOW declarations (domain data that rides on the generic engine) — ordered steps, each routed
# to an agency role. The engine knows none of this; it just instantiates + routes.
WORKFLOWS = [
    # A DAG: assess → desilt → pumps (sequential chain); advisory in parallel after assess;
    # standdown joins on [pumps, advisory]. The interpreter derives readiness from `after`.
    {"id": f"ds:{TENANT}:workflow/flood-response", "title": "Flood response (multi-agency SOP)", "version": "2.0", "steps": [
        {"step_id": "assess",    "title": "Assess ground situation + confirm hotspots", "verb": "fulfil", "assignee_role": "disaster-officer"},
        {"step_id": "desilt",    "title": "Desilt primary drains in affected catchment", "verb": "fulfil", "assignee_role": "sanitation-officer", "after": ["assess"]},
        {"step_id": "pumps",     "title": "Deploy dewatering pumps to hotspots",          "verb": "fulfil", "assignee_role": "water-engineer",     "after": ["desilt"]},
        {"step_id": "advisory",  "title": "Issue public advisory / prepare evacuation",   "verb": "fulfil", "assignee_role": "disaster-officer",  "after": ["assess"]},
        {"step_id": "standdown", "title": "Stand down + file response report",            "verb": "fulfil", "assignee_role": "disaster-officer",  "after": ["pumps", "advisory"]}]},
    {"id": f"ds:{TENANT}:workflow/pump-deployment", "title": "Pump deployment (Water Dept)", "version": "2.0", "steps": [
        {"step_id": "mobilize", "title": "Mobilise pump units",               "verb": "fulfil", "assignee_role": "water-engineer"},
        {"step_id": "deploy",   "title": "Deploy + commission pumps on site",  "verb": "fulfil", "assignee_role": "water-engineer", "after": ["mobilize"]}]},
    {"id": f"ds:{TENANT}:workflow/drain-desilting", "title": "Drain desilting (Sanitation)", "version": "2.0", "steps": [
        {"step_id": "mobilize", "title": "Mobilise desilting crew", "verb": "fulfil", "assignee_role": "sanitation-officer"},
        {"step_id": "desilt",   "title": "Desilt primary drains",   "verb": "fulfil", "assignee_role": "sanitation-officer", "after": ["mobilize"]}]},
]
VIZ = [
    {"id": f"ds:{TENANT}:viz/layer/flood-risk", "layer_id": "flood-risk", "label": "Flood risk", "category": "natural",
     "order": 3, "source_schema": "risk.flood", "render": "map", "value_field": "score", "color_scale": "reds",
     "higher_is_worse": True, "short_label": "Risk", "interpretation": "Deep red = high ponding risk."},
    {"id": f"ds:{TENANT}:viz/layer/alert-card", "layer_id": "alert-card", "label": "Alert", "order": 1,
     "source_schema": "signal.alert", "render": "card", "title_field": "title",
     "fields": [{"path": "severity", "label": "Severity"}, {"path": "area", "label": "Area"},
                {"path": "trigger", "label": "Trigger"}, {"path": "count", "label": "Cells"}, {"path": "status", "label": "Status"}]},
    {"id": f"ds:{TENANT}:viz/layer/forecast-card", "layer_id": "forecast-card", "label": "Forecast", "order": 1,
     "source_schema": "met.forecast", "render": "card", "title_field": "source",
     "fields": [{"path": "rainfall_mm", "label": "Rainfall (mm)"}, {"path": "horizon_h", "label": "Horizon (h)"},
                {"path": "peak_rate_mmph", "label": "Peak (mm/h)"}, {"path": "issued_at", "label": "Issued"}]},
    {"id": f"ds:{TENANT}:viz/layer/guideline-card", "layer_id": "guideline-card", "label": "Guideline", "order": 1,
     "source_schema": "knowledge.guideline", "render": "card", "title_field": "title", "subtitle_field": "citation",
     "fields": [{"path": "proposed_action", "label": "Prescribes"}, {"path": "trigger_level", "label": "At level"}, {"path": "citation", "label": "Cite"}]},
    # recommendation card gains a "Cites" field so the citation shows on the receipt
    {"id": f"ds:{TENANT}:viz/layer/rec-card", "layer_id": "rec-card", "label": "Recommendation", "order": 1,
     "source_schema": "governance.recommendation", "render": "card", "title_field": "title", "subtitle_field": "summary",
     "fields": [{"path": "proposed_action", "label": "Action"}, {"path": "confidence", "label": "Confidence"},
                {"path": "method", "label": "Method"}, {"path": "citation", "label": "Cites"},
                {"path": "sop_workflow", "label": "Triggers SOP"}, {"path": "produced_by", "label": "By"}]},
    {"id": f"ds:{TENANT}:viz/layer/order-card", "layer_id": "order-card", "label": "Order", "order": 1,
     "source_schema": "governance.order", "render": "card", "title_field": "title", "subtitle_field": "instruction",
     "fields": [{"path": "status", "label": "Status"}, {"path": "directed_to", "label": "Directed to"},
                {"path": "workflow", "label": "Workflow"}, {"path": "issued_by", "label": "Issued by"}, {"path": "signature.key_id", "label": "Signed by"}]},
]

def seed_reference():
    for g in GUIDELINES:
        put({"schema": "knowledge.guideline", "applies_to": "flood", "source": "Municipal SOPs / NDMA",
             "summary": g["proposed_action"], **{k: g[k] for k in ("id", "title", "trigger_level", "proposed_action", "citation", "sop_workflow")}, "tenant": TENANT})
    for w in WORKFLOWS:
        put({"schema": "platform.workflow", "tenant": TENANT, **w})
    for v in VIZ:
        put({"schema": "viz.layer", "enabled": True, **v})
    print(f"  seeded {len(GUIDELINES)} guidelines + {len(WORKFLOWS)} SOP workflows + {len(VIZ)} viz.layer descriptors")


def main():
    run = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    print(f"flood-forecast pipeline · tenant={TENANT} · rainfall={RAINFALL_MM}mm · run={run}")
    seed_reference()

    # 1) SENSE — weather driver writes a forecast for the AOI
    fc_id = f"ds:{TENANT}:met/forecast/{run}"
    put({"schema": "met.forecast", "id": fc_id, "aoi": AOI_ID, "issued_at": now(), "horizon_h": 24,
         "rainfall_mm": RAINFALL_MM, "peak_rate_mmph": round(RAINFALL_MM / 3.5, 1), "confidence": 0.7,
         "method": "model", "source": "synthetic-met (offline)", "observed_at": now(), "tenant": TENANT})
    print(f"1 SENSE   met.forecast {fc_id}  rainfall={RAINFALL_MM}mm/24h")

    # 2) PREDICT — flood-risk engine: terrain x forecast -> risk.flood per cell
    cells = [c for c in resolve({"schema": "geo.terrain"}) if (c.get("space") or {}).get("boundary")]
    elevs = [c.get("elevation_m", 0) for c in cells] or [0]
    emin, emax = min(elevs), max(elevs)
    rain_factor = min(RAINFALL_MM / 150.0, 1.0)
    risks = []
    for c in cells:
        elev = c.get("elevation_m", 0.0); slope = c.get("slope_deg", 0.0)
        elev_norm = (elev - emin) / ((emax - emin) or 1)
        slope_factor = 1 - min(slope / 15.0, 1.0)
        suscept = 0.62 * (1 - elev_norm) + 0.38 * slope_factor          # terrain susceptibility 0..1
        score = round(suscept * (0.55 + 0.45 * rain_factor), 3)         # rain scales it
        level = "severe" if score >= 0.85 else "high" if score >= 0.70 else "moderate" if score >= 0.50 else "low"
        h3 = (c.get("space") or {}).get("h3", "")
        rf = {"schema": "risk.flood", "id": f"ds:{TENANT}:risk/flood/{h3}", "space": c["space"], "h3_id": h3,
              "score": score, "level": level, "method": "model", "confidence": 0.72,
              "drivers": {"elevation_m": elev, "slope_deg": slope, "rainfall_mm": RAINFALL_MM},
              "forecast": fc_id, "observed_at": now(), "tenant": TENANT}
        put(rf); risks.append(rf)
    breached = sorted([r for r in risks if r["score"] >= THRESHOLD], key=lambda r: -r["score"])
    top = [r["id"] for r in breached[:3]]
    maxscore = breached[0]["score"] if breached else 0
    print(f"2 PREDICT risk.flood for {len(risks)} cells  ·  {len(breached)} >= {THRESHOLD} (max {maxscore})")
    if not breached:
        print("   no cells breach threshold — no alert. (raise RAINFALL_MM to trigger)"); return 0

    # 3) DETECT — threshold rule fires an alert
    severity = "severe" if any(r["level"] == "severe" for r in breached) else "high"
    case_id = f"ds:{TENANT}:case/flood-{run}"
    task_id = f"ds:{TENANT}:task/flood-{run}"
    alert_id = f"ds:{TENANT}:signal/alert/flood-{run}"
    put({"schema": "signal.alert", "id": alert_id, "kind": "flood-risk", "severity": severity,
         "title": f"Flood risk — {len(breached)} low-lying cells", "area": f"AOI {AOI_ID} · {len(breached)} cells",
         "trigger": f"risk.flood.score >= {THRESHOLD}", "threshold": THRESHOLD, "count": len(breached),
         "case": case_id, "status": "open", "opened_at": now(), "tenant": TENANT})
    print(f"3 DETECT  signal.alert {alert_id}  severity={severity}")

    # 4) ADVISE — pull guidelines from the KB (trigger_level <= severity), emit a cited recommendation each
    picked = [g for g in GUIDELINES if LEVEL_RANK[g["trigger_level"]] <= LEVEL_RANK[severity]]
    picked.sort(key=lambda g: -g["confidence"])
    rec_ids = []
    for i, g in enumerate(picked):
        rid = f"ds:{TENANT}:rec/flood-{run}-{i}"
        put({"schema": "governance.recommendation", "id": rid, "case": case_id, "for_task": task_id,
             "title": g["title"], "summary": g["proposed_action"], "proposed_action": g["proposed_action"],
             "rationale": f"{len(breached)} cells breach risk>={THRESHOLD} (max {maxscore}); {g['citation']} prescribes this at {g['trigger_level']}+ risk.",
             "evidence": [g["id"]] + top, "citation": g["citation"], "confidence": g["confidence"],
             "method": "rule", "produced_by": "component://flood-advisor@0.1.0", "score": g["confidence"],
             "sop_workflow": g["sop_workflow"], "created_at": now(), "tenant": TENANT})
        rec_ids.append(rid)
    print(f"4 ADVISE  {len(rec_ids)} recommendations from guidelines (cited): {[g['citation'] for g in picked]}")

    # open the case + decision task + inbox item that the decision loop consumes
    put({"schema": "platform.case", "id": case_id, "title": f"Flood response — {len(breached)} low-lying cells",
         "service": f"ds:{TENANT}:solution/flood-response", "workflow": f"ds:{TENANT}:workflow/flood",
         "status": "active", "current_step": "decide", "opened_by": "component://flood-advisor@0.1.0",
         "opened_at": now(), "updated_at": now(), "subjects": top + [alert_id],
         "history": [
            {"at": now(), "event": "opened", "actor": "component://flood-advisor@0.1.0", "ref": alert_id, "summary": f"Opened from flood alert (severity {severity})"},
            {"at": now(), "event": "predicted", "actor": "component://flood-risk@0.1.0", "ref": fc_id, "summary": f"{len(breached)} cells predicted >= {THRESHOLD} risk"},
            {"at": now(), "event": "task_created", "actor": "component://flood-advisor@0.1.0", "summary": f"{len(rec_ids)} guideline-based recommendations generated"}],
         "tenant": TENANT})
    put({"schema": "platform.task", "id": task_id, "case": case_id, "verb": "decide",
         "title": "Decide flood response — choose a course of action", "subjects": top,
         "assignee": DECIDER, "workflow_step": "decide", "decision_kind": "select", "options": rec_ids,
         "actions": ["approve", "reject"], "status": "open", "created_at": now(), "tenant": TENANT})
    notify({"actor": DECIDER, "kind": "task", "tenant": TENANT,
            "title": f"Decide flood response — {len(breached)} low-lying cells",
            "body": f"Alert {severity}. {len(rec_ids)} guideline-based options await your decision.",
            "ref": task_id, "source": "flood-forecast", "actions": ["approve", "reject"]})
    print(f"   → case {case_id} + decision task {task_id} + inbox item for {DECIDER}")
    print("DONE — the flood decision now awaits the authority in the Inbox (Slice C decides + signs it).")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rainfall", type=float, help="forecast rainfall mm/24h (default 125; try 40 for no-alert)")
    a = ap.parse_args()
    if a.rainfall is not None:
        RAINFALL_MM = a.rainfall
    raise SystemExit(main())
