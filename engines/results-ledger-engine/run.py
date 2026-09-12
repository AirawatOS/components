"""Results Ledger Engine — the outlay → output → outcome thread (ADR-0071/0072).

Closes the loop inside the implementing agency: money spent (finance.transaction) → thing delivered (project.output)
→ need re-measured & attributed (project.outcome). Every link carries who·what·when·where·why·how, so a funder
reading the progress subscription sees ₹ released → units built here → people moved above the floor there.
"""
import time as _t

CID = "ds:airawatdeveloper:engine/results-ledger-engine"
FIN = "ds:airawatdeveloper:engine/finance-engine"
def _now(): return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
def _slug(s): return str(s).split("/")[-1]

def _project_budget(ctx, project):
    for b in ctx["kfind"]("finance.budget", {"project": project}):
        return b
    return None

def _latest_proposal(ctx, project):
    props = [p for p in ctx["kfind"]("project.proposal") if p.get("project") == project]
    return sorted(props, key=lambda p: str(p.get("version", "")))[-1] if props else None

# ---------------------------------------------------------------- OUTLAY → OUTPUT
def h_record_output(ctx):
    tenant, sub, body = ctx["tenant"], ctx.get("sub"), ctx.get("body") or {}
    project = body.get("project")
    otype = body.get("output_type"); qty = body.get("quantity"); unit = body.get("unit")
    if not project or not otype or qty is None or not unit:
        return {"error": "project + output_type + quantity + unit required"}, None
    now = _now(); key = body.get("key") or f"{_slug(otype)}-{_slug(body.get('milestone') or 'm1')}"
    oid = f"ds:{tenant}:project/output/{_slug(project)}-{key}"
    cost = body.get("cost_cr")
    out = {"schema": "project.output", "id": oid, "tenant": tenant, "project": project,
           "proposal": body.get("proposal"), "milestone": body.get("milestone"),
           "intervention": body.get("intervention"), "output_type": otype,
           "quantity": float(qty), "unit": unit, "delivered_at": now,
           "space": body.get("space"), "cell": body.get("cell"), "ward": body.get("ward"),
           "serves_subject": body.get("serves_subject"), "cost_cr": cost,
           "answers": body.get("answers"), "results_ref": body.get("results_ref"), "status": "delivered",
           "prov": {"method": body.get("method") or "ILLUSTRATIVE — field-reported delivery; replace with works-completion record",
                    "actor": sub, "source": "results-record-output"}}
    ctx["kwrite"](oid, out)

    # OUTLAY joint: post the spend as a balanced double-entry transaction against the project budget
    tx = None; spent = None; fin_err = None
    if cost:
        budget = body.get("budget") or (_project_budget(ctx, project) or {}).get("id")
        if budget:
            res, err = ctx["call"](FIN, "finance-post", {
                "direction": "expenditure", "amount": float(cost), "budget": budget, "contra_type": "liability",
                "project": project, "memo": f"output: {otype} x{qty} {unit}", "source": oid,
                "source_kind": "project.output", "status": "posted"})
            if res: tx, spent = res.get("posted"), res.get("committed")
            else: fin_err = err
    return {"output": oid, "cost_cr": cost, "transaction": tx, "posted_to_budget": spent, "finance_note": fin_err}, None

# ---------------------------------------------------------------- OUTPUT → OUTCOME
def h_close_outcome(ctx):
    tenant, sub, body = ctx["tenant"], ctx.get("sub"), ctx.get("body") or {}
    project = body.get("project")
    if not project:
        return {"error": "project required"}, None
    prop = None
    if body.get("proposal"):
        prop = ctx["kget"](body["proposal"])
    prop = prop or _latest_proposal(ctx, project)
    cells = set((((prop or {}).get("site") or {}).get("cells")) or body.get("cells") or [])
    if not cells:
        return {"error": "no site cells on the proposal/body to re-measure"}, None
    ppl = float(body.get("people_per_unit", 4.5))

    # BASELINE — real: sum persons in the project's cells from welfare.priority (worst-off cell measure)
    base = 0.0; matched = 0
    for p in ctx["kfind"]("welfare.priority"):
        h3 = ((p.get("space") or {}).get("h3")) or p.get("cell")
        if h3 in cells:
            base += float(p.get("persons") or 0); matched += 1
    if base <= 0:                                              # fall back to the proposal's declared outcome baseline
        oc = ((prop or {}).get("outcomes") or [{}])[0]
        base = float(oc.get("baseline") or 0)

    # RELIEF — from the delivered outputs attributed to this project
    outs = [o for o in ctx["kfind"]("project.output", {"project": project})]
    relieved = 0.0
    for o in outs:
        q = float(o.get("quantity") or 0); t = str(o.get("output_type") or "")
        if o.get("serves_persons") is not None:
            relieved += float(o["serves_persons"])
        elif "dwelling" in t or "unit" in t:
            relieved += q * ppl
        elif "cell" in t and cells:
            relieved += (q / len(cells)) * base                # treated a fraction of the cells → proportional relief
        else:
            relieved += q * ppl
    relieved = min(relieved, base)
    actual = max(0.0, base - relieved)

    now = _now(); oid = f"ds:{tenant}:project/outcome/{_slug(project)}"
    oc_ref = (((prop or {}).get("outcomes") or [{}])[0]).get("id")
    outcome = {"schema": "project.outcome", "id": oid, "tenant": tenant, "project": project,
               "indicator": body.get("indicator") or "persons_below_floor",
               "capability": body.get("capability"), "subject": project, "subject_kind": "project",
               "value": round(relieved, 1),                    # the outcome value = persons moved above the floor
               "baseline": round(base, 1), "actual": round(actual, 1), "delta": round(relieved, 1),
               "persons_moved": round(relieved, 1), "unit": "persons",
               "cell": None, "ward": None,
               "attributed_outputs": [o["id"] for o in outs],  # the ATTRIBUTION edge — outcome ⇐ these outputs
               "observed_at": now,
               "prov": {"method": body.get("method") or "PROJECTED — real baseline (welfare.priority) + delivered outputs; replace with post-works re-survey",
                        "actor": sub, "source": "results-close-outcome", "cells_matched": matched}}
    ctx["kwrite"](oid, outcome)
    return {"outcome": oid, "baseline_persons": round(base, 1), "actual_persons_below": round(actual, 1),
            "persons_moved": round(relieved, 1), "attributed_outputs": len(outs), "cells": len(cells)}, None

# ---------------------------------------------------------------- read the whole thread
def h_thread(ctx):
    body = ctx.get("body") or {}; project = body.get("project")
    if not project:
        return {"error": "project required"}, None
    proj = ctx["kget"](project)
    budget = _project_budget(ctx, project)
    outs = [o for o in ctx["kfind"]("project.output", {"project": project})]
    # prefer the proposal the delivered work was actually booked against; else the latest
    prop_id = next((o.get("proposal") for o in outs if o.get("proposal")), None)
    prop = (ctx["kget"](prop_id) if prop_id else None) or _latest_proposal(ctx, project)
    outcome = ctx["kget"](f"ds:{ctx['tenant']}:project/outcome/{_slug(project)}")
    rec = None
    for a in ((prop or {}).get("answers") or []):
        if "recommendation" in str(a): rec = ctx["kget"](a); break
    return {
        "need": {"recommendation": (rec or {}).get("id"), "summary": (rec or {}).get("summary")},
        "proposal": {"id": (prop or {}).get("id"), "ask_cr": ((prop or {}).get("ask") or {}).get("amount_cr"),
                     "outputs_planned": (prop or {}).get("outputs"), "outcomes_planned": (prop or {}).get("outcomes")},
        "outlay": {"budget": (budget or {}).get("id"), "allocated_cr": (budget or {}).get("allocated"),
                   "spent_cr": (budget or {}).get("spent"), "committed_cr": (budget or {}).get("committed"),
                   "committed_revenue_cr": (budget or {}).get("committed_revenue"), "realized_revenue_cr": (budget or {}).get("realized_revenue")},
        "outputs": [{"id": o["id"], "type": o.get("output_type"), "qty": o.get("quantity"), "unit": o.get("unit"),
                     "cost_cr": o.get("cost_cr")} for o in outs],
        "outcome": {"id": (outcome or {}).get("id"), "baseline": (outcome or {}).get("baseline"),
                    "actual": (outcome or {}).get("actual"), "persons_moved": (outcome or {}).get("persons_moved"),
                    "attributed_outputs": (outcome or {}).get("attributed_outputs")},
    }, None

HANDLERS = {"results-record-output": h_record_output, "results-close-outcome": h_close_outcome, "results-thread": h_thread}

def _selftest():
    store = {}
    prio = [{"id": f"ds:x:welfare/priority/c{i}", "space": {"h3": f"cell{i}"}, "persons": 1000} for i in range(3)]
    prop = {"id": "ds:x:project/proposal/p--v1", "project": "ds:x:project/p", "version": "1",
            "site": {"cells": ["cell0", "cell1", "cell2"]}, "outcomes": [{"id": "o1", "baseline": 3000}]}
    bud = {"id": "ds:x:finance/budget/b", "project": "ds:x:project/p", "allocated": 100, "spent": 0, "account": "ds:x:finance/account/exp"}
    db = {"project.proposal": [prop], "welfare.priority": prio, "finance.budget": [bud], "project.output": []}
    def kfind(s, where=None):
        rows = db.get(s, []) + [v for v in store.values() if v.get("schema") == s]
        if where: rows = [r for r in rows if all(r.get(k) == v for k, v in where.items())]
        return rows
    def call(tgt, verb, payload):  # fake finance-post
        return {"posted": "ds:x:finance/transaction/t1", "committed": payload["amount"]}, None
    ctx = {"tenant": "x", "sub": "je1", "kwrite": lambda i, o: store.__setitem__(i, o),
           "kfind": kfind, "kget": lambda i: store.get(i) or next((r for r in db.get("project.proposal", []) if r["id"] == i), None),
           "call": call}
    r1, _ = h_record_output(dict(ctx, body={"project": "ds:x:project/p", "output_type": "dwelling_unit",
                                            "quantity": 100, "unit": "units", "cost_cr": 40, "budget": "ds:x:finance/budget/b"}))
    assert r1["transaction"] and store[r1["output"]]["status"] == "delivered", r1
    r2, _ = h_close_outcome(dict(ctx, body={"project": "ds:x:project/p", "people_per_unit": 4.5}))
    assert abs(r2["persons_moved"] - 450) < 1 and r2["baseline_persons"] == 3000 and r2["attributed_outputs"] == 1, r2
    assert store[r2["outcome"]]["attributed_outputs"] == [r1["output"]], "attribution edge missing"
    print("selftest OK — output:", _slug(r1["output"]), "| tx posted | outcome moved", r2["persons_moved"], "of", r2["baseline_persons"])

def main():
    print("Results Ledger Engine — handlers:", ", ".join(sorted(HANDLERS))); _selftest(); return 0

if __name__ == "__main__":
    raise SystemExit(main())
