"""Effectiveness Engine (aos SDK rewrite) — the ASSESS edge of the decision loop.

Behaviourally identical to components/engines/effectiveness-engine, rebuilt on the `aos`
SDK to show the boilerplate it removes: no ctx-dict plumbing, no (result, err) tuples,
no hand-built HANDLERS, no manual reads/writes — the SDK tracks them and derives meta.
"""
from aos import engine, ctx

LOWER_IS_BETTER = {"cell.crowd_density": True, "risk.flood": True, "cell.crush_risk": True}
ACTION_TARGET = {"reduce-inflow": ("cell.crowd_density", "density_sqm"),
                 "thin-crowd-and-relocate": ("cell.crowd_density", "density_sqm")}

_NOTE = ("OBSERVED change on the affected cells (peak that justified acting → latest observation). "
         "This is the measured outcome, NOT a causal claim — attribution to the intervention alone "
         "would require a counterfactual (a comparable un-treated area or a predicted no-action baseline).")


def _h3(o):
    g = o.get("space") or o.get("cell_ref") or {}
    return g.get("h3_id") or g.get("h3")


@engine.verb("assess-effectiveness", implements="intelligence.assess_effectiveness@0.1.0")
def assess_effectiveness(body):
    """Assess one decision (body.decision) or the latest approved decisions."""
    want = body.get("decision")
    decs = ctx.facts("governance.decision")
    decs = (decs.where(lambda d: d.get("id") == want) if want
            else decs.where(lambda d: d.get("outcome") == "approved").recent("decided_at"))
    out = []
    for d in list(decs)[:20]:
        r = _assess_one(d)
        if r:
            out.append({"decision": d.get("id"), "direction": r["direction"], "delta_pct": r["delta_pct"],
                        "baseline": r["baseline"]["mean"], "followup": r["followup"]["mean"]})
    return {"assessed": len(out), "results": out}


def _assess_one(decision):
    recs = ctx.facts("governance.recommendation").by_id()
    rec_ids = (decision.get("evidence_manifest") or decision.get("considered")
               or ([decision.get("selected")] if decision.get("selected") else []))
    action = target = None
    affected = set()
    for rid in rec_ids:
        r = recs.get(rid)
        if not r:
            continue
        action = action or r.get("proposed_action")
        target = target or ACTION_TARGET.get(r.get("proposed_action"))
        for ev in (r.get("evidence") or []):
            h = ev.rsplit("/", 1)[-1].split("-")[0]
            if h and len(h) >= 8:
                affected.add(h)
    if not target:
        target = ("cell.crowd_density", "density_sqm")
    tschema, tfield = target
    if not affected:
        return None

    series = {}
    for o in ctx.facts(tschema):
        h = _h3(o)
        if h in affected:
            series.setdefault(h, []).append(o)
    if not series:
        return None

    base_vals, follow_vals, base_at, follow_at = [], [], "", ""
    for rows in series.values():
        rows2 = [r for r in rows if isinstance(r.get(tfield), (int, float))]
        if not rows2:
            continue
        peak = max(rows2, key=lambda r: r.get(tfield) or 0)
        latest = max(rows2, key=lambda r: r.get("observed_at") or "")
        base_vals.append(peak.get(tfield) or 0)
        follow_vals.append(latest.get(tfield) or 0)
        base_at = max(base_at, peak.get("observed_at") or "")
        follow_at = max(follow_at, latest.get("observed_at") or "")
    if not base_vals:
        return None

    base = round(sum(base_vals) / len(base_vals), 3)
    follow = round(sum(follow_vals) / len(follow_vals), 3)
    delta = round(follow - base, 3)
    delta_pct = round(100.0 * delta / base, 1) if base else None
    lower_better = LOWER_IS_BETTER.get(tschema, True)
    improved = (delta < 0) if lower_better else (delta > 0)
    direction = ("improved" if (improved and abs(delta_pct or 0) >= 5)
                 else ("worsened" if (not improved and abs(delta_pct or 0) >= 5) else "unchanged"))
    did = decision.get("id")
    base_id = (did or "decision").rsplit("/", 1)[-1]
    n = ctx.now()
    return ctx.write({
        "schema": "intelligence.effectiveness", "id": f"ds:{ctx.tenant}:intelligence/effectiveness/{base_id}",
        "decision": did, "intervention": action, "target_schema": tschema, "target_field": tfield,
        "affected_cells": len(affected), "goal": ("lower" if lower_better else "higher"),
        "baseline": {"at": base_at, "mean": base}, "followup": {"at": follow_at, "mean": follow},
        "delta": delta, "delta_pct": delta_pct, "direction": direction,
        "method": "observed-before-after", "confidence": 0.6, "note": _NOTE, "observed_at": n,
        "prov": {"component": "component://ds:airawatdeveloper:engine/effectiveness-engine",
                 "method": "observed-before-after", "at": n}})


HANDLERS = engine.handlers()   # ← app_host finds this unchanged; SDK built it from the decorator
