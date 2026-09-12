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
CVER = "0.1.4"
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
        _verb = "Relieve" if (caps.get(o["cap"]) or {}).get("floor_direction") == "lower_is_better" else "Raise"
        rid = f"ds:{tenant}:governance/recommendation/welfare-{_slug(o['cap'].split('/')[-1])}-{_slug(o['ward'].split('/')[-1])}"
        pick = "PRIORITISE" if o["selected"] else f"DEFER ({o.get('deferred_reason', 'budget')})"
        rationale = (f"At least {o['persons_conf_below']:,} people (90% CI; point estimate "
                     f"{o['persons_below_point']:,}) are below the {cap_label} floor in this area, at coverage "
                     f"{o['coverage']}. Mean shortfall depth {o['depth']}; mean deprivation of the affected "
                     f"{o['equity']} (0-1). Ranked #{rank} by confident-persons-below × depth × equity "
                     f"(prioritarian — the worse-off count more). Provisional estimates were excluded (ADR-0070).")
        ctx["kwrite"](rid, {
            "schema": "governance.recommendation", "id": rid, "tenant": tenant,
            "title": f"{_verb} {cap_label} — {o['ward'].split('/')[-1]}",
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

def h_prioritise_cells(ctx):
    """CELL-GRAIN prioritisation (ADR-0069): don't roll up to a city scalar — rank the individual per-cell gaps
    worst-off-first and knapsack the budget over CELLS, so 'DEFER city' becomes 'fund THESE cells that fit the
    budget; defer the rest'. Writes welfare.priority per cell (rank + fundability, map-able) + one summary
    governance.recommendation per capability naming where the money goes and which constraint bound it."""
    tenant = ctx["tenant"]; now = _now(); body = ctx.get("body") or {}
    budget_cr = body.get("fiscal_budget_cr")                  # optional fiscal ceiling
    cap_filter = body.get("capability")                       # optional: rank one capability
    write_top = int(body.get("write_top", 1500))              # cap welfare.priority writes (worst-first)
    top_k_reco = int(body.get("top_k", 5))

    gaps = ctx["kfind"]("welfare.gap") or []
    floors = {f.get("capability"): f for f in (ctx["kfind"]("welfare.floor") or [])}
    caps = {c.get("id"): c for c in (ctx["kfind"]("welfare.capability") or [])}
    dep_by = {d.get("subject"): d.get("index", 0.0) for d in (ctx["kfind"]("welfare.deprivation") or [])}
    hh_by = {h.get("id"): h for h in (ctx["kfind"]("civic.household") or [])}
    cost_models = {c.get("capability"): c for c in (ctx["kfind"]("welfare.cost_model") or [])}
    unit_cost = {cap: cm["fiscal_cr_per_person"] for cap, cm in cost_models.items() if cm.get("fiscal_cr_per_person") is not None}
    unit_cost.update(body.get("unit_cost_cr_per_person") or {})
    env_draw_pp = {}
    for cap, cm in cost_models.items():
        ed = (cm.get("env_draws") or [])
        if ed and ed[0].get("resource"): env_draw_pp[cap] = {"resource": ed[0]["resource"], "amount": ed[0].get("amount_per_person")}
    # ADR-0069 Step 2 — INTERVENTION CATALOGUE: pick the recommended lever per capability (least environmental
    # draw, then least cost) and cost each cell by THAT lever instead of the naive cost model. A land-light lever
    # (vertical redevelopment) funds far more cells than a land-hungry one — this is where 'what to do' bites.
    levers = {}
    for iv in (ctx["kfind"]("welfare.intervention") or []):
        levers.setdefault(iv.get("capability"), []).append(iv)
    chosen = {}
    _big = 9e9
    for cap, lv in levers.items():
        best = sorted(lv, key=lambda x: (x.get("env_draw_per_person", _big) if x.get("env_draw_per_person") is not None else _big,
                                         x.get("unit_cost_cr_per_person", _big) if x.get("unit_cost_cr_per_person") is not None else _big))[0]
        chosen[cap] = best
        if best.get("unit_cost_cr_per_person") is not None: unit_cost[cap] = best["unit_cost_cr_per_person"]
        if best.get("env_resource"): env_draw_pp[cap] = {"resource": best["env_resource"], "amount": best.get("env_draw_per_person")}
    unit_cost.update(body.get("unit_cost_cr_per_person") or {})     # explicit what-if body override still wins
    env_draw_pp.update(body.get("env_draw_per_person") or {})

    # environmental headroom per resource (regen - draw)
    head = {}
    for b in (ctx["kfind"]("env.resource_budget") or []):
        h = b.get("headroom")
        if h is None:
            rr, cd = b.get("regen_rate"), b.get("current_draw")
            h = (rr - cd) if (rr is not None and cd is not None) else None
        if h is not None: head[b.get("resource")] = {"headroom": h, "unit": b.get("unit"), "id": b.get("id")}

    # build the per-cell candidate list: every unit genuinely below its floor (shortfall > 0)
    cells = []
    for gp in gaps:
        cap = gp.get("capability"); sf = gp.get("shortfall") or 0.0
        if cap_filter and cap != cap_filter: continue
        if not sf or sf <= 0: continue                        # only units below the dignity floor
        subj = gp.get("subject"); persons = gp.get("weight") or (hh_by.get(subj) or {}).get("size") or 0
        fv = gp.get("floor_value") or (floors.get(cap) or {}).get("threshold")
        depth = (sf / fv) if fv else 0.0
        deprivation = dep_by.get(subj, 0.0)
        score = persons * (1.0 + depth) * (1.0 + deprivation)
        ed = env_draw_pp.get(cap) or {}
        cells.append({"subject": subj, "cap": cap, "gap_id": gp.get("id"),
                      "space": (hh_by.get(subj) or {}).get("space") or {"h3": subj.rsplit("cell-", 1)[-1] if "cell-" in subj else None},
                      "persons": persons, "depth": round(depth, 3), "deprivation": round(deprivation, 3),
                      "score": round(score, 2), "shortfall": sf, "floor_value": fv, "level_value": gp.get("level_value"),
                      "cost_cr": round((unit_cost.get(cap, 0) or 0) * persons, 4),
                      "env_resource": ed.get("resource"), "env_draw": round((ed.get("amount") or 0) * persons, 4)})
    cells.sort(key=lambda c: -c["score"])

    # knapsack worst-off-first: fund until the fiscal ceiling OR the resource headroom is exhausted
    spend = 0.0; used = {r: 0.0 for r in head}; funded_n = 0; funded_persons = 0.0
    for c in cells:
        reason = None
        if budget_cr is not None and c["cost_cr"] and spend + c["cost_cr"] > budget_cr:
            reason = "budget-fiscal"
        r = c.get("env_resource")
        if reason is None and r in head and c["env_draw"] and used[r] + c["env_draw"] > head[r]["headroom"]:
            reason = f"budget-env:{r}"
        if reason:
            c["funded"] = False; c["budget_binding"] = reason
        else:
            c["funded"] = True; c["budget_binding"] = None
            spend += (c["cost_cr"] or 0)
            if r in head: used[r] += (c["env_draw"] or 0)
            funded_n += 1; funded_persons += (c["persons"] or 0)
        c["cum_cost"] = round(spend, 3); c["cum_env"] = round(used.get(r, 0.0), 3) if r in used else None

    # per-capability summary recommendation (decision spine): WHERE the money goes + the binding constraint
    by_cap = {}
    for c in cells: by_cap.setdefault(c["cap"], []).append(c)
    reco_written = 0; reco_ref = {}
    for cap, cc in by_cap.items():
        capo = caps.get(cap) or {}
        cap_label = capo.get("label", cap.split("/")[-1])
        # direction-aware verb: a ceiling capability (lower_is_better, e.g. crowding) is RELIEVED, not "raised".
        lower_better = capo.get("floor_direction") == "lower_is_better"
        verb = "Relieve" if lower_better else "Raise"
        toward = "out of overcrowding" if lower_better else "above the floor"
        lever = chosen.get(cap) or {}
        lever_txt = (f" Recommended lever: {lever.get('label')} (from the intervention catalogue"
                     + (f", part-funded via {lever['from_scheme']}" if lever.get("from_scheme") else "") + ").") if lever else ""
        fnd = [c for c in cc if c["funded"]]; deferred = [c for c in cc if not c["funded"]]
        persons_below = round(sum(c["persons"] for c in cc)); persons_fund = round(sum(c["persons"] for c in fnd))
        binding = deferred[0]["budget_binding"] if deferred else None
        rid = f"ds:{tenant}:governance/recommendation/welfare-cells-{_slug(cap.split('/')[-1])}"
        reco_ref[cap] = rid
        head_top = ", ".join((c["space"] or {}).get("h3", "?") for c in cc[:3])
        rationale = (f"{len(cc):,} cells are below the {cap_label} dignity floor ({persons_below:,} people {toward}). Ranked "
                     f"worst-off-first by persons x depth x deprivation. Within the stated budget, {len(fnd):,} cells "
                     f"({persons_fund:,} people) are FUNDABLE now; {len(deferred):,} cells defer" +
                     (f" (binding constraint: {binding})" if binding else "") +
                     f". Worst-off cells: {head_top}." + lever_txt + " Act cell-by-cell, not city-wide (ADR-0069 cell-grain).")
        ctx["kwrite"](rid, {
            "schema": "governance.recommendation", "id": rid, "tenant": tenant,
            "title": f"{verb} {cap_label} — {len(fnd):,} priority cells",
            "summary": (f"{verb} {cap_label} for the worst-off {len(fnd):,} cells ({persons_fund:,} people)"
                        + (f"; {len(deferred):,} defer ({binding})" if binding else "; all cells fundable") + "."
                        + (f" Lever: {lever.get('label')}." if lever else "")),
            "proposed_action": (f"{('Relieve' if lower_better else 'Raise')} {cap_label} at the {len(fnd):,} funded cells "
                                f"(see welfare.priority, funded=true), worst-off first" +
                                (f", via {lever.get('label')}" if lever else "") +
                                f". {len(deferred):,} cells await budget/carrying-capacity."),
            "rationale": rationale, "evidence": [c["gap_id"] for c in cc[:20]],
            "confidence": 0.6, "score": round(sum(c["score"] for c in fnd), 1),
            "method": "statistical", "produced_by": CID, "created_at": now})
        reco_written += 1

    # write welfare.priority per cell (worst-first, capped) — the map-able 'what to do where'
    written = 0
    for rank, c in enumerate(cells[:write_top], 1):
        pid = f"ds:{tenant}:welfare/priority/{_slug(c['cap'].split('/')[-1])}-{(c['space'] or {}).get('h3') or _slug(c['subject'].split('/')[-1])}"
        ctx["kwrite"](pid, {
            "schema": "welfare.priority", "id": pid, "tenant": tenant, "subject": c["subject"],
            "subject_kind": "household", "observed_at": now, "capability": c["cap"], "space": c["space"],
            "rank": rank, "score": c["score"], "persons": c["persons"], "depth": c["depth"],
            "deprivation": c["deprivation"], "shortfall": c["shortfall"], "floor_value": c["floor_value"],
            "level_value": c["level_value"], "cost_cr": c["cost_cr"], "env_resource": c["env_resource"],
            "env_draw": c["env_draw"], "funded": c["funded"], "budget_binding": c["budget_binding"],
            "cumulative_cost_cr": c["cum_cost"], "cumulative_env_draw": c["cum_env"],
            "gap_ref": c["gap_id"], "recommendation_ref": reco_ref.get(c["cap"]),
            "method": "derived", "confidence": 0.6,
            "derived_from": [c["gap_id"]],
            "prov": {"method": "cell-grain worst-off-first rank + budget knapsack (persons x depth x deprivation)",
                     "produced_by": {"component": {"id": CID, "version": CVER}}, "observed_at": now}})
        written += 1

    return {"cells_below_floor": len(cells), "priority_written": written, "recommendations": reco_written,
            "funded_cells": funded_n, "funded_persons": round(funded_persons),
            "budget_cr": budget_cr, "env_headroom": head,
            "top": cells[:5]}, None

HANDLERS = {"welfare-prioritise": h_prioritise, "welfare-prioritise-cells": h_prioritise_cells}

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

def _selftest_cells():
    # 3 overcrowded cells (A worst: dense+deprived), 1 below-floor-but-fine; tiny land budget funds only the worst
    gaps = [
      {"id": "ds:x:welfare/gap/d-A", "capability": "welfare.capability/density", "subject": "ds:x:civic/household/cell-A",
       "shortfall": 300, "floor_value": 200, "weight": 1000, "level_value": 500},
      {"id": "ds:x:welfare/gap/d-B", "capability": "welfare.capability/density", "subject": "ds:x:civic/household/cell-B",
       "shortfall": 100, "floor_value": 200, "weight": 800, "level_value": 300},
      {"id": "ds:x:welfare/gap/d-C", "capability": "welfare.capability/density", "subject": "ds:x:civic/household/cell-C",
       "shortfall": 50, "floor_value": 200, "weight": 200, "level_value": 250},
      {"id": "ds:x:welfare/gap/d-D", "capability": "welfare.capability/density", "subject": "ds:x:civic/household/cell-D",
       "shortfall": 0, "floor_value": 200, "weight": 400, "level_value": 10}]                # not below floor → excluded
    floors = [{"id": "ds:x:welfare/floor/density", "capability": "welfare.capability/density", "threshold": 200}]
    caps = [{"id": "welfare.capability/density", "label": "Residential crowding", "service_unit": "persons_per_ha"}]
    hh = [{"id": f"ds:x:civic/household/cell-{k}", "size": s, "space": {"h3": k}} for k, s in
          [("A", 1000), ("B", 800), ("C", 200), ("D", 400)]]
    dep = [{"subject": "ds:x:civic/household/cell-A", "index": 0.8}, {"subject": "ds:x:civic/household/cell-B", "index": 0.3}]
    cm = [{"capability": "welfare.capability/density", "fiscal_cr_per_person": 0.005,
           "env_draws": [{"resource": "land", "amount_per_person": 0.001}]}]
    env = [{"resource": "land", "regen_rate": 200, "current_draw": 199, "unit": "ha", "id": "ds:x:env/b/land"}]  # 1 ha headroom
    store = {}
    ctx = {"tenant": "x", "body": {}, "kwrite": lambda i, o: store.__setitem__(i, o),
           "kfind": lambda s: {"welfare.gap": gaps, "welfare.floor": floors, "welfare.capability": caps,
                               "civic.household": hh, "welfare.deprivation": dep, "welfare.cost_model": cm,
                               "env.resource_budget": env}.get(s, [])}
    res, err = h_prioritise_cells(ctx)
    assert err is None and res["cells_below_floor"] == 3, res                 # D excluded (not below floor)
    assert res["top"][0]["space"]["h3"] == "A", res["top"]                    # A worst-off (dense+deep+deprived)
    # 1 ha headroom / 0.001 ha per person = 1000 persons of land → only cell A (1000 ppl) funds; B/C defer on land
    assert res["funded_cells"] == 1 and res["funded_persons"] == 1000, res
    prio = [o for o in store.values() if o["schema"] == "welfare.priority"]
    reco = [o for o in store.values() if o["schema"] == "governance.recommendation"]
    assert len(prio) == 3 and len(reco) == 1, (len(prio), len(reco))
    a = next(o for o in prio if o["space"]["h3"] == "A")
    assert a["rank"] == 1 and a["funded"] is True, a
    b = next(o for o in prio if o["space"]["h3"] == "B")
    assert b["funded"] is False and b["budget_binding"] == "budget-env:land", b
    print("cell selftest OK:", {k: res[k] for k in ("cells_below_floor", "funded_cells", "funded_persons", "recommendations")})
    print("  reco summary:", reco[0]["summary"])

def main():
    print("Welfare Prioritisation Engine — handlers:", ", ".join(sorted(HANDLERS)))
    _selftest(); _selftest_cells(); return 0

if __name__ == "__main__":
    raise SystemExit(main())
