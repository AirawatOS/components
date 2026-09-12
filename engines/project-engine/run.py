#!/usr/bin/env python3
"""Project Engine — the results-contract lifecycle: propose → approve → execute → review → evaluate → adjust.

A project binds why→how→proof→cost (OUTCOME ◄ OUTPUT ◄ DELIVERABLE─MILESTONE ◄ OUTLAY→BUDGET), embedded in a VERSIONED
project.proposal = the baseline. Every transition that changes the contract is a DECIDED CHANGE-REQUEST (ADR-0056):
approval writes a governance.decision (the engine never decides — the authority = the approver). Actuals are REAL posted
finance.transactions tagged with `project` (no fabrication) — the project is the roll-up axis (also by owning org_unit).

Handlers: proposal-submit · proposal-decide · project-rollup · project-review · project-evaluate · project-adjust.
Writes with THIS engine's grants (component://project-engine obo user).
"""
import time as _t, json as _json

_j = _json.dumps                                          # an inbox action-spec, carried as a JSON string in inbox_item.actions


def _now():
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())


def _sum_outlay(outlay):
    return round(sum((o.get("amount") or 0) for o in (outlay or [])), 2)


def _proposals_for(ctx, pid):
    return [p for p in ctx["kfind"]("project.proposal") if p.get("project") == pid]


def _next_version(ctx, pid):
    vs = [p.get("version") or 0 for p in _proposals_for(ctx, pid)]
    return (max(vs) + 1) if vs else 1


def _in_force(ctx, proj):
    """The approved baseline currently in force (project.baseline_version), or None."""
    bv = proj.get("baseline_version")
    if not bv:
        return None
    return next((p for p in _proposals_for(ctx, proj["id"]) if p.get("version") == bv), None)


def _actuals(ctx, pid):
    """REAL posted finance.transactions tagged to this project → (spent, revenue, by_type is from the plan)."""
    spent = rev = 0.0
    for t in ctx["kfind"]("finance.transaction", {"project": pid}):
        if t.get("status") == "void":
            continue
        amt = t.get("amount") or 0
        if t.get("direction") == "revenue":
            rev += amt
        else:
            spent += amt
    return round(spent, 2), round(rev, 2)


def _milestone_progress(baseline):
    ms = (baseline or {}).get("milestones") or []
    if not ms:
        return None
    done = sum(1 for m in ms if m.get("status") in ("done", "completed", "achieved"))
    return round(100.0 * done / len(ms), 1)


def _route_inbox(ctx, tenant, prid, code, version, name, total, now, sponsor=None):
    """Route a submitted proposal to the approver's Inbox (sponsor if a real user, else the tenant-admin role)."""
    approver = sponsor if (sponsor and str(sponsor).startswith("user://")) else f"role://{tenant}/platform.tenant-admin"
    iid = f"ds:{tenant}:platform/inbox_item/proposal-{code}-v{version}"
    ctx["kwrite"](iid, {"schema": "platform.inbox_item", "id": iid, "tenant": tenant, "actor": approver,
        "title": f"Project proposal — {code} v{version}", "body": f"{name} · outlay {total}",
        "ref": prid, "kind": "task", "status": "unread", "created_at": now,
        "actions": [_j({"label": "Approve", "verb": "proposal-decide", "style": "pri", "args": {"project": f"ds:{tenant}:project/{code}", "version": version, "decision": "approve"}}),
                    _j({"label": "Reject", "verb": "proposal-decide", "style": "danger", "args": {"project": f"ds:{tenant}:project/{code}", "version": version, "decision": "reject"}})]})
    return approver


# --- 1) PROPOSE ----------------------------------------------------------------------------------------------
def h_proposal_submit(ctx):
    """Author/submit a project proposal (a new baseline version, status=submitted). Upserts the thin project entity."""
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    pj = body.get("project") or {}
    prop = body.get("proposal") or {}
    code = pj.get("code") or body.get("code")
    if not code:
        return None, "project code required"
    outcomes, outputs = prop.get("outcomes") or [], prop.get("outputs") or []
    if not outcomes:
        return None, "a proposal needs at least one OUTCOME (the why)"
    if not outputs:
        return None, "a proposal needs at least one OUTPUT (the how)"
    outlay = prop.get("outlay") or []
    total = _sum_outlay(outlay)
    as_draft = bool(body.get("as_draft"))                    # ADR-0071: leave as an editable DRAFT (no Inbox routing yet)
    new_status = "draft" if as_draft else "submitted"
    pid = f"ds:{tenant}:project/{code}"
    now = _now()
    existing = ctx["kget"](pid) or {}
    ctx["kwrite"](pid, {"schema": "project.project", "id": pid, "tenant": tenant, "code": code,
        "name": pj.get("name") or existing.get("name") or code, "org_unit": pj.get("org_unit") or existing.get("org_unit"),
        "objective": pj.get("objective") or (outcomes[0].get("statement") if outcomes else None),
        "sponsor": pj.get("sponsor") or existing.get("sponsor"),
        "start_date": pj.get("start_date") or existing.get("start_date"), "end_date": pj.get("end_date") or existing.get("end_date"),
        "status": new_status, "baseline_version": existing.get("baseline_version"),
        "currency": pj.get("currency") or existing.get("currency") or "INR", "created_at": existing.get("created_at") or now})
    version = _next_version(ctx, pid)
    prid = f"ds:{tenant}:project/proposal/{code}--v{version}"
    prop_rec = {"schema": "project.proposal", "id": prid, "tenant": tenant, "project": pid,
        "version": version, "status": new_status, "title": prop.get("title") or f"{code} proposal v{version}",
        "summary": prop.get("summary"), "outcomes": outcomes, "outputs": outputs,
        "milestones": prop.get("milestones") or [], "deliverables": prop.get("deliverables") or [], "outlay": outlay,
        "total_outlay": total, "currency": pj.get("currency") or "INR", "submitted_by": sub, "submitted_at": now,
        "supersedes": (existing.get("baseline_version") if body.get("adjust") else None), "created_at": now}
    # ADR-0071: pass through the optional funding-pivot fields when a drafter supplies them (schema 0.2.0)
    for k in ("answers", "evidence", "site", "intervention", "ask"):
        if prop.get(k) is not None:
            prop_rec[k] = prop[k]
    ctx["kwrite"](prid, prop_rec)
    # DRAFT stays with the author (no routing); SUBMITTED routes the approval to the Inbox.
    routed = None if as_draft else _route_inbox(ctx, tenant, prid, code, version,
        pj.get("name") or existing.get("name") or code, total, now, pj.get("sponsor") or existing.get("sponsor"))
    return {"project": pid, "proposal": prid, "version": version, "total_outlay": total, "status": new_status, "routed_to": routed}, None


def h_proposal_save(ctx):
    """JE authoring (ADR-0071): edit a DRAFT proposal in place — the human refines the AI pre-fill (intervention lever,
    budget, ask, feasibility narrative). submit:true flips draft→submitted and routes the approval to the Inbox."""
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    pid, version = body.get("project"), body.get("version")
    if not pid or version is None:
        return None, "project + version required"
    code = pid.rsplit("/", 1)[-1]
    prid = f"ds:{tenant}:project/proposal/{code}--v{version}"
    prop = ctx["kget"](prid)
    if not prop:
        return None, f"proposal not found: {prid}"
    if prop.get("status") not in ("draft", "submitted"):
        return None, f"cannot edit a {prop.get('status')} proposal — adjust an approved baseline instead"
    patch = body.get("patch") or {}
    for k in ("title", "summary", "outcomes", "outputs", "milestones", "deliverables", "outlay",
              "answers", "evidence", "site", "intervention", "ask"):
        if k in patch:
            prop[k] = patch[k]
    prop["total_outlay"] = _sum_outlay(prop.get("outlay"))
    now = _now()
    submit = bool(body.get("submit"))
    if submit:
        prop["status"] = "submitted"; prop["submitted_by"] = sub; prop["submitted_at"] = now
    ctx["kwrite"](prid, prop)
    routed = None
    proj = ctx["kget"](pid) or {}
    if submit:
        if proj:
            proj["status"] = "submitted"; ctx["kwrite"](pid, proj)
        routed = _route_inbox(ctx, tenant, prid, code, version, proj.get("name") or code, prop["total_outlay"], now,
                              proj.get("sponsor"))
    return {"proposal": prid, "status": prop["status"], "total_outlay": prop["total_outlay"], "routed_to": routed}, None


# --- 2) APPROVE / REJECT (decided change-request → governance.decision) ---------------------------------------
def h_proposal_decide(ctx):
    """Approve or reject a submitted proposal. Approve → governance.decision + activate project + commit the budget
    (outlay → finance.budget). Reject → proposal+project rejected. Gate: an admin or the project's sponsor."""
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    pid = body.get("project")
    if not pid:
        return None, "project id required"
    proj = ctx["kget"](pid)
    if not proj:
        return None, "project not found"
    version = body.get("version")
    props = _proposals_for(ctx, pid)
    prop = (next((p for p in props if p.get("version") == version), None) if version
            else next((p for p in props if p.get("status") == "submitted"), None))
    if not prop:
        return None, "no submitted proposal to decide"
    roles = set(ctx["claims"].get("roles", []))
    is_admin = bool(roles & {"platform.tenant-admin", "platform.steward", "admin"})
    if not (is_admin or proj.get("sponsor") in (sub, ctx["claims"].get("sub"))):
        return None, "only an admin or the project sponsor may decide this proposal"
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        return None, "decision must be approve or reject"
    now = _now()
    did = f"ds:{tenant}:governance/decision/proj-{proj['code']}-v{prop['version']}"
    ctx["kwrite"](did, {"schema": "governance.decision", "id": did, "tenant": tenant,
        "case": pid, "task": prop["id"],                      # decided change-request: the project / its proposal
        "outcome": "approved" if decision == "approve" else "rejected",
        "authority": sub, "decided_at": now, "rationale": body.get("rationale") or f"{decision} project {proj['code']} baseline v{prop['version']}",
        "subjects": [pid], "considered": [prop["id"]], "evidence_manifest": [prop["id"]]})
    # close the approver's Inbox task + notify the submitter of the outcome
    iid = f"ds:{tenant}:platform/inbox_item/proposal-{proj['code']}-v{prop['version']}"
    prior_i = ctx["kget"](iid)
    if prior_i:
        ctx["kwrite"](iid, {**prior_i, "status": "done"})
    if prop.get("submitted_by"):
        nid = f"ds:{tenant}:platform/inbox_item/proposalres-{proj['code']}-v{prop['version']}"
        ctx["kwrite"](nid, {"schema": "platform.inbox_item", "id": nid, "tenant": tenant, "actor": prop["submitted_by"],
            "title": f"Project proposal {'approved' if decision == 'approve' else 'rejected'} — {proj['code']}",
            "body": body.get("rationale") or "", "ref": prop["id"], "kind": "notification", "status": "unread", "created_at": now})
    if decision == "reject":
        ctx["kwrite"](prop["id"], {**prop, "status": "rejected", "decision": did, "reason": body.get("rationale")})
        ctx["kwrite"](pid, {**proj, "status": "rejected"})
        return {"decided": "reject", "project": pid, "version": prop["version"], "decision": did}, None
    # APPROVE: supersede the prior in-force baseline, activate, commit the budget from outlay
    if proj.get("baseline_version"):
        prior = next((p for p in props if p.get("version") == proj["baseline_version"]), None)
        if prior and prior["id"] != prop["id"]:
            ctx["kwrite"](prior["id"], {**prior, "status": "superseded"})
    ctx["kwrite"](prop["id"], {**prop, "status": "approved", "decision": did, "approved_at": now})
    ctx["kwrite"](pid, {**proj, "status": "active", "baseline_version": prop["version"]})
    bcode = body.get("budget_code") or f"{proj['code']}-budget"
    bid = f"ds:{tenant}:finance/budget/{bcode}"
    prior_bud = ctx["kget"](bid) or {}
    ctx["kwrite"](bid, {"schema": "finance.budget", "id": bid, "tenant": tenant, "code": bcode,
        "name": f"{proj.get('name', proj['code'])} budget", "fiscal_year": body.get("fiscal_year") or now[:4],
        "account": body.get("account") or prior_bud.get("account"), "org_unit": proj.get("org_unit"), "project": pid,
        "allocated": prop.get("total_outlay") or 0, "committed": prior_bud.get("committed", 0) or 0,
        "spent": prior_bud.get("spent", 0) or 0, "currency": prop.get("currency") or "INR", "status": "active",
        "created_at": prior_bud.get("created_at") or now})
    return {"decided": "approve", "project": pid, "version": prop["version"], "decision": did,
            "status": "active", "budget": bid, "allocated": prop.get("total_outlay")}, None


# --- 3) ROLLUP: Plan vs Actual (by project, and by org_unit) -------------------------------------------------
def h_project_rollup(ctx):
    """Plan (approved outlay/budget) vs Actual (real posted transactions), for one project or the whole portfolio,
    with a by-resource-type plan split and an optional group-by owning org_unit."""
    body, tenant = ctx["body"], ctx["tenant"]
    one = body.get("project")
    projects = [ctx["kget"](one)] if one else ctx["kfind"]("project.project")
    projects = [p for p in projects if p]
    budgets = {b.get("project"): b for b in ctx["kfind"]("finance.budget") if b.get("project")}
    rows = []
    for proj in projects:
        base = _in_force(ctx, proj)
        spent, rev = _actuals(ctx, proj["id"])
        bud = budgets.get(proj["id"]) or {}
        allocated = bud.get("allocated", base.get("total_outlay") if base else 0) or 0
        by_type = {}
        for o in (base or {}).get("outlay", []):
            by_type[o.get("resource_type", "other")] = round(by_type.get(o.get("resource_type", "other"), 0) + (o.get("amount") or 0), 2)
        rows.append({"project": proj["id"], "code": proj.get("code"), "name": proj.get("name"),
            "org_unit": proj.get("org_unit"), "status": proj.get("status"), "baseline_version": proj.get("baseline_version"),
            "plan_outlay": (base or {}).get("total_outlay") or 0, "allocated": allocated,
            "committed": bud.get("committed", 0) or 0, "spent": spent, "revenue": rev,
            "available": round(allocated - (bud.get("committed", 0) or 0) - spent, 2),
            "variance": round(allocated - spent, 2), "plan_by_type": by_type,
            "milestone_progress": _milestone_progress(base)})
    by_unit = {}
    for r in rows:
        u = r.get("org_unit") or "(unassigned)"
        agg = by_unit.setdefault(u, {"org_unit": u, "projects": 0, "allocated": 0, "spent": 0, "revenue": 0})
        agg["projects"] += 1; agg["allocated"] = round(agg["allocated"] + r["allocated"], 2)
        agg["spent"] = round(agg["spent"] + r["spent"], 2); agg["revenue"] = round(agg["revenue"] + r["revenue"], 2)
    return {"projects": rows, "by_unit": list(by_unit.values()),
            "totals": {"allocated": round(sum(r["allocated"] for r in rows), 2),
                       "spent": round(sum(r["spent"] for r in rows), 2),
                       "revenue": round(sum(r["revenue"] for r in rows), 2)}}, None


# --- 4) REVIEW (periodic progress vs plan) -------------------------------------------------------------------
def h_project_review(ctx):
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    pid = body.get("project")
    proj = ctx["kget"](pid) if pid else None
    if not proj:
        return None, "project not found"
    if not proj.get("baseline_version"):
        return None, "project has no approved baseline to review"
    base = _in_force(ctx, proj)
    spent, _rev = _actuals(ctx, pid)
    bud = next((b for b in ctx["kfind"]("finance.budget", {"project": pid})), {})
    allocated = (bud.get("allocated") if bud else None) or (base.get("total_outlay") if base else 0) or 0
    mp = _milestone_progress(base)
    variance = round(allocated - spent, 2)
    if allocated and spent > allocated:
        status = "off_track"
    elif allocated and spent > 0.8 * allocated and (mp is None or mp < 80):
        status = "at_risk"
    elif allocated:
        status = "on_track"
    else:
        status = "unknown"
    now = _now()
    rid = f"ds:{tenant}:project/review/{proj['code']}--{now.replace(':', '').replace('-', '')}"
    rec = {"schema": "project.review", "id": rid, "tenant": tenant, "project": pid, "at": now,
        "period": body.get("period"), "milestone_progress": mp, "planned_to_date": allocated,
        "spent_to_date": spent, "variance": variance, "status": status, "narrative": body.get("narrative"),
        "flags": body.get("flags") or [], "reviewed_by": sub, "created_at": now}
    ctx["kwrite"](rid, rec)
    ctx["kwrite"](pid, {**proj, "status": "review"})
    return {"review": rid, "status": status, "spent_to_date": spent, "planned_to_date": allocated,
            "variance": variance, "milestone_progress": mp}, None


# --- 5) EVALUATE (outcomes achieved? outputs delivered? value for money?) ------------------------------------
def h_project_evaluate(ctx):
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    pid = body.get("project")
    proj = ctx["kget"](pid) if pid else None
    if not proj:
        return None, "project not found"
    if not proj.get("baseline_version"):
        return None, "project has no approved baseline to evaluate"
    rec = body.get("recommendation")
    if rec not in ("continue", "close", "scale", "redesign"):
        return None, "recommendation must be continue|close|scale|redesign"
    base = _in_force(ctx, proj)
    spent, _rev = _actuals(ctx, pid)
    now = _now()
    eid = f"ds:{tenant}:project/evaluation/{proj['code']}--{now.replace(':', '').replace('-', '')}"
    ctx["kwrite"](eid, {"schema": "project.evaluation", "id": eid, "tenant": tenant, "project": pid, "at": now,
        "outcomes_assessment": body.get("outcomes_assessment") or [], "outputs_delivered": body.get("outputs_delivered") or [],
        "outlay_planned": (base or {}).get("total_outlay") or 0, "outlay_actual": spent,
        "value_for_money": body.get("value_for_money"), "rating": body.get("rating") or "unknown",
        "lessons": body.get("lessons") or [], "recommendation": rec, "evaluator": sub, "created_at": now})
    ctx["kwrite"](pid, {**proj, "status": "closed" if rec == "close" else "evaluation"})
    return {"evaluation": eid, "recommendation": rec, "outlay_planned": (base or {}).get("total_outlay") or 0,
            "outlay_actual": spent, "project_status": "closed" if rec == "close" else "evaluation"}, None


# --- 6) ADJUST (re-baseline → new version, back through decide) ----------------------------------------------
def h_project_adjust(ctx):
    """Propose a revised baseline (changed outlay/milestones/outputs). Same path as submit but flagged adjust — the new
    version supersedes the in-force one ONLY on approval (proposal-decide). Sets the project back to submitted."""
    ctx["body"]["adjust"] = True
    pj = ctx["body"].get("project")
    if isinstance(pj, str):                                    # accept a project id; re-hydrate its code for submit
        proj = ctx["kget"](pj) or {}
        ctx["body"]["project"] = {"code": proj.get("code"), "name": proj.get("name"), "org_unit": proj.get("org_unit"),
            "objective": proj.get("objective"), "sponsor": proj.get("sponsor"),
            "start_date": proj.get("start_date"), "end_date": proj.get("end_date"), "currency": proj.get("currency")}
    return h_proposal_submit(ctx)


# --- ALLOCATIONS (people → project %; the basis for labour apportionment) ------------------------------------
def h_allocation_set(ctx):
    """Assign an employee to a project at a % of time (or end an assignment). Idempotent per (employee, project)."""
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    emp, proj, pct = body.get("employee"), body.get("project"), body.get("percent")
    if not emp or not proj:
        return None, "employee and project required"
    if not isinstance(pct, (int, float)) or pct < 0 or pct > 100:
        return None, "percent must be 0–100"
    local = f"{emp.rsplit('/', 1)[-1]}--{proj.rsplit('/', 1)[-1]}"
    aid = f"ds:{tenant}:project/allocation/{local}"
    now = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
    prior = ctx["kget"](aid) or {}
    ctx["kwrite"](aid, {"schema": "project.allocation", "id": aid, "tenant": tenant, "employee": emp, "project": proj,
        "percent": pct, "role_on_project": body.get("role_on_project"), "from_date": body.get("from_date") or prior.get("from_date") or now[:10],
        "to_date": body.get("to_date"), "status": "ended" if body.get("end") else "active",
        "created_at": prior.get("created_at") or now})
    return {"allocation": aid, "employee": emp, "project": proj, "percent": pct, "status": "ended" if body.get("end") else "active"}, None


def h_allocation_list(ctx):
    proj = ctx["body"].get("project")
    rows = [a for a in ctx["kfind"]("project.allocation") if (not proj or a.get("project") == proj)]
    return {"allocations": rows}, None


HANDLERS = {"proposal-submit": h_proposal_submit, "proposal-save": h_proposal_save, "proposal-decide": h_proposal_decide,
            "project-rollup": h_project_rollup, "project-review": h_project_review,
            "project-evaluate": h_project_evaluate, "project-adjust": h_project_adjust,
            "allocation-set": h_allocation_set, "allocation-list": h_allocation_list}


def main():
    print("Project Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
