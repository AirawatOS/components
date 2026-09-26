#!/usr/bin/env python3
"""Effectiveness Engine — the ASSESS edge of the decision loop.

Given a signed governance.decision that dispatched an intervention, measure the TARGET MEASURE on the affected
cells before vs after the intervention, and write an intelligence.effectiveness record: baseline (the peak that
justified acting), follow-up (the latest observation), the delta, and a direction verdict against the goal
(for crowd density / flood risk, DOWN is better). HONEST: it reports the OBSERVED change on the affected cells
and records that causal attribution requires a counterfactual — it never claims the intervention alone caused it.
In-process HANDLERS engine (ctx surface only). RUN_TOKEN lazy so it imports for lifecycle/HANDLERS dispatch.
"""
import os as _os, json as _json, time as _t

# goal per target measure: does a LOWER value mean the situation improved?
LOWER_IS_BETTER = {"cell.crowd_density": True, "risk.flood": True, "cell.crush_risk": True}
# which measure a proposed_action is trying to move
ACTION_TARGET = {"reduce-inflow": ("cell.crowd_density", "density_sqm"),
                 "thin-crowd-and-relocate": ("cell.crowd_density", "density_sqm")}


def _now(): return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
def _h3(o):
    g = o.get("space") or o.get("cell_ref") or {}
    return g.get("h3_id") or g.get("h3")


def assess(ctx, decision):
    kfind = ctx["kfind"]; tenant = ctx["tenant"]
    action = None
    # resolve the intervention + affected cells from the decision's recommendations
    rec_ids = decision.get("evidence_manifest") or decision.get("considered") or ([decision.get("selected")] if decision.get("selected") else [])
    recs = {r.get("id"): r for r in kfind("governance.recommendation")}
    affected = set(); target = None
    for rid in rec_ids:
        r = recs.get(rid)
        if not r:
            continue
        action = action or r.get("proposed_action")
        tgt = ACTION_TARGET.get(r.get("proposed_action"))
        if tgt:
            target = target or tgt
        for ev in (r.get("evidence") or []):
            # evidence ids look like ds:tenant:cell/crowd_density/<h3>-<time> or risk/flood/<h3>
            tail = ev.rsplit("/", 1)[-1]
            h = tail.split("-")[0]
            if h and len(h) >= 8:
                affected.add(h)
    if not target:
        target = ("cell.crowd_density", "density_sqm")
    tschema, tfield = target
    if not affected:
        return None, "no affected cells resolvable from the decision's recommendations"

    # gather the target measure series on the affected cells
    series = {}
    for o in kfind(tschema):
        h = _h3(o)
        if h in affected:
            series.setdefault(h, []).append(o)
    if not series:
        return None, f"no {tschema} observations on the affected cells"

    # baseline = the PEAK that justified acting; follow-up = the LATEST observation, per cell — then average
    base_vals, follow_vals = [], []
    base_at, follow_at = "", ""
    for h, rows in series.items():
        rows2 = [r for r in rows if isinstance(r.get(tfield), (int, float))]
        if not rows2:
            continue
        peak = max(rows2, key=lambda r: r.get(tfield) or 0)
        latest = max(rows2, key=lambda r: r.get("observed_at") or "")
        base_vals.append(peak.get(tfield) or 0); follow_vals.append(latest.get(tfield) or 0)
        base_at = max(base_at, peak.get("observed_at") or ""); follow_at = max(follow_at, latest.get("observed_at") or "")
    if not base_vals:
        return None, "no numeric observations to compare"
    base = round(sum(base_vals) / len(base_vals), 3)
    follow = round(sum(follow_vals) / len(follow_vals), 3)
    delta = round(follow - base, 3)
    delta_pct = round(100.0 * delta / base, 1) if base else None
    lower_better = LOWER_IS_BETTER.get(tschema, True)
    improved = (delta < 0) if lower_better else (delta > 0)
    direction = "improved" if (improved and abs(delta_pct or 0) >= 5) else ("worsened" if (not improved and abs(delta_pct or 0) >= 5) else "unchanged")

    now = _now(); did = decision.get("id"); base_id = (did or "decision").rsplit("/", 1)[-1]
    eid = f"ds:{tenant}:intelligence/effectiveness/{base_id}"
    obj = {"schema": "intelligence.effectiveness", "id": eid, "tenant": tenant,
           "decision": did, "intervention": action, "target_schema": tschema, "target_field": tfield,
           "affected_cells": len(affected), "goal": ("lower" if lower_better else "higher"),
           "baseline": {"at": base_at, "mean": base}, "followup": {"at": follow_at, "mean": follow},
           "delta": delta, "delta_pct": delta_pct, "direction": direction,
           "method": "observed-before-after", "confidence": 0.6,
           "note": ("OBSERVED change on the affected cells (peak that justified acting → latest observation). "
                    "This is the measured outcome, NOT a causal claim — attribution to the intervention alone "
                    "would require a counterfactual (a comparable un-treated area or a predicted no-action baseline)."),
           "observed_at": now,
           "prov": {"component": "component://ds:airawatdeveloper:engine/effectiveness-engine", "method": "observed-before-after", "at": now}}
    ctx["kwrite"](eid, obj)
    return obj, None


def h_assess(ctx):
    """assess-effectiveness verb: assess one decision (body.decision) or the latest actionable decision."""
    kfind = ctx["kfind"]; body = ctx.get("body") or {}
    decs = kfind("governance.decision")
    want = body.get("decision")
    if want:
        decs = [d for d in decs if d.get("id") == want]
    else:
        decs = [d for d in decs if d.get("outcome") == "approved"]
        decs.sort(key=lambda d: d.get("decided_at") or "", reverse=True)
    out = []
    for d in decs[:20]:
        r, err = assess(ctx, d)
        if r:
            out.append({"decision": d.get("id"), "direction": r["direction"], "delta_pct": r["delta_pct"],
                        "baseline": r["baseline"]["mean"], "followup": r["followup"]["mean"]})
    return {"assessed": len(out), "results": out}, None


HANDLERS = {"assess-effectiveness": h_assess}

if __name__ == "__main__":
    print("Effectiveness Engine — verb assess-effectiveness (did the intervention work? observed before/after).")
