"""Welfare Prioritisation Engine (ADR-0069 slice 2; ADR-0046 hourglass; ADR-0029 spine).

Reads the publishable welfare gap estimates and ranks them into recommendations — floor-first, prioritarian,
honest under uncertainty:

  priority = persons_confidently_below_floor  ×  (1 + depth)          [× budget feasibility]

  * persons_confidently_below = ci_low × persons_total  — the LOWER credible bound, so we prioritise the gap we
    are SURE about, not the optimistic point estimate. (Anti-cherry-pick.)
  * depth = mean shortfall / floor threshold for that capability — how far below, not just how many.
  * PROVISIONAL estimates (publishable=false, ADR-0070) are excluded entirely — we never act on a number the
    platform itself won't stand behind.
  * optional budget: pass body {"fiscal_budget_cr", "unit_cost_cr_per_person": {capability: cost}} → greedy
    select by persons-helped-per-rupee until the budget is spent; the rest are flagged deferred(budget).

Emits one governance.recommendation per opportunity (a PROPOSED option, evidence-linked) — a human authority
admits one into a Decision. The engine decides nothing.
"""
import time as _t

CID = "ds:airawatdeveloper:engine/welfare-prioritisation-engine"
CVER = "0.1.3"
def _now(): return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
def _slug(s): return "".join(c if c.isalnum() else "-" for c in str(s)).strip("-").lower()[:48] or "x"

def h_prioritise(ctx):
    tenant = ctx["tenant"]; now = _now(); body = ctx.get("body") or {}
    top_k = int(body.get("top_k", 10))
    budget_cr = body.get("fiscal_budget_cr")                 # optional fiscal ceiling

    estimates = ctx["kfind"]("stat.estimate") or []
    floors = {f.get("capability"): f for f in (ctx["kfind"]("welfare.floor") or [])}
    caps = {c.get("id"): c for c in (ctx["kfind"]("welfare.capability") or [])}
    gaps = ctx["kfind"]("welfare.gap") or []
    dep_by = {d.get("subject"): d.get("index", 0.0) for d in (ctx["kfind"]("welfare.deprivation") or [])}
    # GOVERNED cost model per capability (fiscal ₹cr/person + env draw/person) → the budget bites from data,
    # not hand-config. Body values still override (what-if scenarios).
    cost_models = {c.get("capability"): c for c in (ctx["kfind"]("welfare.cost_model") or [])}
    unit_cost = {cap: cm["fiscal_cr_per_person"] for cap, cm in cost_models.items() if cm.get("fiscal_cr_per_person") is not None}
    unit_cost.update(body.get("unit_cost_cr_per_person") or {})

    # per capability, over the below-floor population: mean shortfall → depth; mean deprivation → equity weight
    sf = {}; dep = {}
    for gp in gaps:
        s = gp.get("shortfall")
        if s and s > 0:
            c = gp.get("capability")
            sf.setdefault(c, []).append(s)
            dep.setdefault(c, []).append(dep_by.get(gp.get("subject"), 0.0))
    mean_sf = {c: (sum(v) / len(v)) for c, v in sf.items()}
    mean_dep = {c: (sum(v) / len(v)) for c, v in dep.items()}   # 0..1 mean deprivation of the below-floor people

    opps = []
    considered = provisional = 0
    for e in estimates:
        if e.get("metric") != "welfare.gap.below_floor_fraction":
            continue
        considered += 1
        if not e.get("publishable"):
            provisional += 1; continue                       # ADR-0070: never act on a provisional number
        cap = e.get("capability"); ward = e.get("subject")
        pv = e.get("prov") or {}
        persons_total = pv.get("persons_total") or 0
        ci_low = e.get("ci_low") or 0.0
        persons_conf_below = ci_low * persons_total          # at-least-this-many below floor (90% CI)
        thr = (floors.get(cap) or {}).get("threshold")
        depth = (mean_sf.get(cap, 0.0) / thr) if thr else 0.0
        equity = mean_dep.get(cap, 0.0)                        # prioritarian: how deprived the below-floor people are (0..1)
        score = persons_conf_below * (1.0 + depth) * (1.0 + equity)
        cost = (unit_cost.get(cap, 0) or 0) * persons_conf_below
        conf = round((e.get("coverage") or 0) * (pv.get("mean_reliability") or 0), 3)
        opps.append({"cap": cap, "ward": ward, "estimate_id": e.get("id"),
                     "floor_id": (floors.get(cap) or {}).get("id"),
                     "persons_below_point": round((e.get("value") or 0) * persons_total),
                     "persons_conf_below": round(persons_conf_below),
                     "ci_low": ci_low, "ci_high": e.get("ci_high"), "coverage": e.get("coverage"),
                     "depth": round(depth, 3), "equity": round(equity, 3), "score": round(score, 1),
                     "cost_cr": round(cost, 2), "confidence": conf})

    opps.sort(key=lambda o: -o["score"])

    # environmental carrying-capacity headroom per resource (regen - draw); an intervention may not overdraw it
    head = {}
    for b in (ctx["kfind"]("env.resource_budget") or []):
        h = b.get("headroom")
        if h is None:
            rr, cd = b.get("regen_rate"), b.get("current_draw")
            h = (rr - cd) if (rr is not None and cd is not None) else None
        if h is not None:
            head[b.get("resource")] = {"headroom": h, "unit": b.get("unit"), "id": b.get("id")}
    env_draw = {}                                               # from the governed cost model; body overrides below
    for cap, cm in cost_models.items():
        ed = cm.get("env_draws") or []
        if ed and ed[0].get("resource"):
            env_draw[cap] = {"resource": ed[0].get("resource"), "amount": ed[0].get("amount_per_person")}
    env_draw.update(body.get("env_draw_per_person") or {})      # {capability: {resource, amount}}
    for o in opps:
        ed = env_draw.get(o["cap"]) or {}
        o["env_resource"] = ed.get("resource")
        o["env_draw"] = round((ed.get("amount") or 0) * o["persons_conf_below"], 2)

    # DUAL greedy budget: fund the highest-priority (worst-off-first) opportunities until EITHER the fiscal
    # ceiling OR the environmental headroom for the resource it draws on is exhausted. No budget → all selected.
    if budget_cr is not None or head:
        spend = 0.0; used = {r: 0.0 for r in head}
        for o in opps:                                         # already sorted by prioritarian score
            reason = None
            if budget_cr is not None and o["cost_cr"] and spend + o["cost_cr"] > budget_cr:
                reason = "budget-fiscal"
            r = o.get("env_resource")
            if reason is None and r in head and o["env_draw"] and used[r] + o["env_draw"] > head[r]["headroom"]:
                reason = f"budget-env:{r}"
            if reason:
                o["selected"] = False; o["deferred_reason"] = reason
            else:
                o["selected"] = True; spend += (o["cost_cr"] or 0)
                if r in head: used[r] += o["env_draw"]
    else:
        for o in opps:
            o["selected"] = True

    # emit a governance.recommendation per opportunity (top_k) — proposed options for a human to admit
    written = 0
    for rank, o in enumerate(opps[:top_k], 1):
        cap_label = (caps.get(o["cap"]) or {}).get("label", o["cap"])
        unit = (caps.get(o["cap"]) or {}).get("service_unit", "")
        rid = f"ds:{tenant}:governance/recommendation/welfare-{_slug(o['cap'].split('/')[-1])}-{_slug(o['ward'].split('/')[-1])}"
        pick = "PRIORITISE" if o["selected"] else f"DEFER ({o.get('deferred_reason', 'budget')})"
        rationale = (f"At least {o['persons_conf_below']:,} people (90% CI; point estimate "
                     f"{o['persons_below_point']:,}) are below the {cap_label} floor in this area, at coverage "
                     f"{o['coverage']}. Mean shortfall depth {o['depth']}; mean deprivation of the affected "
                     f"{o['equity']} (0-1). Ranked #{rank} by confident-persons-below × depth × equity "
                     f"(prioritarian — the worse-off count more). Provisional estimates were excluded (ADR-0070).")
        ctx["kwrite"](rid, {
            "schema": "governance.recommendation", "id": rid, "tenant": tenant,
            "title": f"Raise {cap_label} — {o['ward'].split('/')[-1]}",
            "summary": f"{pick}: ~{o['persons_conf_below']:,} people below the {cap_label} floor.",
            "proposed_action": f"Prioritise a {cap_label} intervention for {o['ward']} "
                               f"(target: bring the ~{o['persons_conf_below']:,} confidently-below-floor residents above {unit}).",
            "rationale": rationale,
            "evidence": [o["estimate_id"], o["floor_id"]],
            "confidence": o["confidence"], "score": o["score"],
            "method": "statistical",   # governance.recommendation.method enum (INT-0001); v1 detail is in rationale
            "produced_by": CID, "created_at": now})
        written += 1

    return {"estimates_considered": considered, "provisional_excluded": provisional,
            "opportunities": len(opps), "recommendations": written,
            "budget_cr": budget_cr, "top": opps[:3]}, None

HANDLERS = {"welfare-prioritise": h_prioritise}

def _selftest():
    # in-memory: two publishable estimates (density: 20k below; water: 5k below) + one provisional (excluded)
    ests = [
      {"schema": "stat.estimate", "id": "ds:x:stat/estimate/d", "metric": "welfare.gap.below_floor_fraction",
       "capability": "welfare.capability/density", "subject": "ds:x:jur/ward/w1", "value": 0.28, "ci_low": 0.19,
       "ci_high": 0.39, "coverage": 1.0, "publishable": True, "prov": {"persons_total": 68000, "mean_reliability": 0.6}},
      {"schema": "stat.estimate", "id": "ds:x:stat/estimate/w", "metric": "welfare.gap.below_floor_fraction",
       "capability": "welfare.capability/water", "subject": "ds:x:jur/ward/w1", "value": 0.5, "ci_low": 0.1,
       "ci_high": 0.9, "coverage": 0.9, "publishable": True, "prov": {"persons_total": 68000, "mean_reliability": 0.7}},
      {"schema": "stat.estimate", "id": "ds:x:stat/estimate/p", "metric": "welfare.gap.below_floor_fraction",
       "capability": "welfare.capability/sanitation", "subject": "ds:x:jur/ward/w1", "value": 0.7, "ci_low": 0.1,
       "ci_high": 0.95, "coverage": 0.2, "publishable": False, "prov": {"persons_total": 68000}}]
    floors = [{"id": "ds:x:welfare/floor/density", "capability": "welfare.capability/density", "threshold": 200},
              {"id": "ds:x:welfare/floor/water", "capability": "welfare.capability/water", "threshold": 135}]
    caps = [{"id": "welfare.capability/density", "label": "Residential crowding", "service_unit": "persons_per_ha"},
            {"id": "welfare.capability/water", "label": "Water", "service_unit": "LPCD"}]
    gaps = [{"capability": "welfare.capability/density", "shortfall": 50}, {"capability": "welfare.capability/water", "shortfall": 60}]
    store = {}
    ctx = {"tenant": "x", "body": {}, "kwrite": lambda i, o: store.__setitem__(i, o),
           "kfind": lambda s: {"stat.estimate": ests, "welfare.floor": floors, "welfare.capability": caps, "welfare.gap": gaps,
                               "welfare.deprivation": [{"subject": "ds:x:civic/household/h3", "index": 0.6}], "env.resource_budget": [], "welfare.cost_model": []}.get(s, [])}
    res, err = h_prioritise(ctx)
    assert err is None and res["provisional_excluded"] == 1 and res["recommendations"] == 2, res
    # density: 0.19*68000=12920 confidently below; water: 0.1*68000=6800. density should rank #1.
    assert res["top"][0]["cap"].endswith("density"), res["top"]
    assert len(store) == 2 and all(o["schema"] == "governance.recommendation" for o in store.values())
    print("selftest OK:", {k: res[k] for k in ("estimates_considered", "provisional_excluded", "recommendations")})
    print("  #1:", {k: res["top"][0][k] for k in ("cap", "persons_conf_below", "depth", "score")})

def main():
    print("Welfare Prioritisation Engine — handlers:", ", ".join(sorted(HANDLERS)))
    _selftest(); return 0

if __name__ == "__main__":
    raise SystemExit(main())
