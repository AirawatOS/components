#!/usr/bin/env python3
"""HR Workflows Engine — the server-side LOGIC for leave / attendance / expense self-service + approvals.

The host invokes HANDLERS[verb](ctx) for the mapped /bridge verbs. `ctx` is the engine's I/O + helper surface,
attributed component://hr-workflows obo the signed-in user (ADR-0052 — this engine holds the write grants):
    ctx["tenant"], ctx["sub"], ctx["body"]           — request context
    ctx["kwrite"](id, obj) / kfind(schema, where) / kget(id)   — registry I/O (engine-attributed)
    ctx["policy_resolve"](domain, employee_id=None)  — the policy engine
    ctx["finance_post"](body) -> (result, err)       — the double-entry settlement seam
    ctx["first_account"](acct_type)                  — chart lookup (e.g. Payables contra)
    ctx["leave_balance"](leave_type, employee_id)    — accrual-aware leave balance
Every handler returns (result_dict, None) on success or (None, "error string") on failure.
Edit this file in Studio to change HR behaviour, then Publish → Compose → upgrade the solution.
"""
import time as _t, uuid as _u


def _now():
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())


POLICY_ENG = "ds:airawatdeveloper:engine/policy-engine"
FINANCE_ENG = "ds:airawatdeveloper:engine/finance-engine"


def _me(ctx):
    """The signed-in user's org.employee record (linked by their login), or None."""
    return next((e for e in ctx["kfind"]("org.employee") if e.get("citizen") == ctx["sub"]), None)


def _leave_balance(ctx, ltype, applicant_login):
    """Policy-driven leave balance for a type: accrued (via the policy engine) − approved − pending this year.
    Returns None if no policy caps that type (e.g. unpaid = uncapped)."""
    res, err = ctx["call"](POLICY_ENG, "policy-resolve", {"domain": "leave"})
    if err or not res:
        return None
    rules = next(((p.get("rules") or {}) for p in res.get("policies", []) if (p.get("rules") or {}).get("leave_type") == ltype), None)
    if not rules or rules.get("annual_entitlement") is None:
        return None
    g = _t.gmtime(); yr = str(g.tm_year)
    ent = rules["annual_entitlement"]
    if rules.get("accrual_per_month"):
        ent = min(ent, round(rules["accrual_per_month"] * g.tm_mon, 1))
    reqs = [l for l in ctx["kfind"]("hr.leave_request") if l.get("applicant_login") == applicant_login
            and l.get("leave_type") == ltype and str(l.get("from_date", "")).startswith(yr)]
    taken = sum(l.get("days", 0) or 0 for l in reqs if l.get("status") == "approved")
    pending = sum(l.get("days", 0) or 0 for l in reqs if l.get("status") == "pending")
    return {"entitlement": rules["annual_entitlement"], "accrued": ent, "taken": taken, "pending": pending,
            "available": max(0, ent - taken - pending)}


def h_leave_apply(ctx):
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    me = _me(ctx)
    if not me:
        return None, "no employee record is linked to your login — ask your admin to set your Login user in Org Setup"
    mgr = next((e for e in ctx["kfind"]("org.employee") if e.get("id") == me.get("manager")), None)
    mgr_login = (mgr or {}).get("citizen")
    ltype, reqd = body.get("leave_type", "casual"), body.get("days") or 0
    bal = _leave_balance(ctx, ltype, sub)                       # policy-driven balance (via the policy engine); None = uncapped
    if bal and reqd > bal["available"]:
        return None, (f"only {bal['available']} day(s) of {ltype} leave available "
                      f"(accrued {bal['accrued']} - taken {bal['taken']} - pending {bal['pending']}); requested {reqd}")
    now = _now(); lid = f"ds:{tenant}:hr/leave_request/{_u.uuid4().hex[:8]}"
    leave = {"schema": "hr.leave_request", "id": lid, "tenant": tenant, "employee": me["id"], "applicant_login": sub,
             "manager": (mgr or {}).get("id"), "manager_login": mgr_login, "leave_type": ltype,
             "from_date": body.get("from_date"), "to_date": body.get("to_date"), "days": body.get("days"),
             "reason": body.get("reason"), "status": "pending",
             "scope": ({"org_unit": me.get("org_unit")} if me.get("org_unit") else None), "created_at": now}
    ctx["kwrite"](lid, leave)
    routed = None
    if mgr_login:
        iid = f"ds:{tenant}:platform/inbox_item/leave-{lid.rsplit('/', 1)[-1]}"
        ctx["kwrite"](iid, {"schema": "platform.inbox_item", "id": iid, "tenant": tenant, "actor": mgr_login,
            "title": f"Leave request — {me.get('name') or me['id']}",
            "body": f"{ltype} · {leave['from_date']} → {leave['to_date']}", "ref": lid, "kind": "task",
            "status": "unread", "created_at": now})
        routed = mgr_login
    return {"filed": lid, "status": "pending", "routed_to": routed, "manager": (mgr or {}).get("name")}, None


def h_leave_decide(ctx):
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    lid, decision = body.get("leave_id"), body.get("decision")
    if decision not in ("approve", "reject"):
        return None, "decision must be approve|reject"
    lv = ctx["kget"](lid) if lid else None
    if not lv:
        return None, "leave request not found"
    if lv.get("manager_login") != sub:
        return None, "only the applicant's manager may decide this leave"
    now = _now(); status = "approved" if decision == "approve" else "rejected"
    ctx["kwrite"](lid, {**lv, "status": status, "decided_by": sub, "decided_at": now})
    if lv.get("applicant_login"):
        nid = f"ds:{tenant}:platform/inbox_item/leaveres-{lid.rsplit('/', 1)[-1]}"
        ctx["kwrite"](nid, {"schema": "platform.inbox_item", "id": nid, "tenant": tenant, "actor": lv["applicant_login"],
            "title": f"Leave {status}", "body": f"{lv.get('leave_type')} {lv.get('from_date')} → {lv.get('to_date')}",
            "ref": lid, "kind": "notification", "status": "unread", "created_at": now})
    iid = f"ds:{tenant}:platform/inbox_item/leave-{lid.rsplit('/', 1)[-1]}"
    prior = ctx["kget"](iid)
    if prior:
        ctx["kwrite"](iid, {**prior, "status": "done"})
    return {"decided": decision, "status": status, "leave": lid}, None


def h_attendance_mark(ctx):
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    now = _t.gmtime()
    date = body.get("date") or _t.strftime("%Y-%m-%d", now); hhmm = _t.strftime("%H:%M", now)
    me = _me(ctx)
    if not me:
        return None, "no employee record is linked to your login"
    aid = f"ds:{tenant}:hr/attendance/{me['id'].rsplit('/', 1)[-1]}-{date}"
    rec = ctx["kget"](aid) or {"schema": "hr.attendance", "id": aid, "tenant": tenant, "employee": me["id"],
                               "date": date, "status": "present", "created_at": _now()}
    act = body.get("action")
    if act == "check_in": rec["check_in"] = hhmm
    elif act == "check_out": rec["check_out"] = hhmm
    if body.get("status"): rec["status"] = body["status"]
    ctx["kwrite"](aid, rec)
    return {"marked": aid, "date": date, "check_in": rec.get("check_in"), "check_out": rec.get("check_out"),
            "status": rec.get("status")}, None


def h_expense_apply(ctx):
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    if not isinstance(body.get("amount"), (int, float)) or body["amount"] <= 0:
        return None, "a positive amount is required"
    me = _me(ctx)
    if not me:
        return None, "no employee record is linked to your login"
    mgr = next((e for e in ctx["kfind"]("org.employee") if e.get("id") == me.get("manager")), None)
    mgr_login = (mgr or {}).get("citizen")
    now = _now(); eid = f"ds:{tenant}:hr/expense_claim/{_u.uuid4().hex[:8]}"
    claim = {"schema": "hr.expense_claim", "id": eid, "tenant": tenant, "employee": me["id"], "applicant_login": sub,
             "manager": (mgr or {}).get("id"), "manager_login": mgr_login, "category": body.get("category"),
             "amount": body["amount"], "currency": body.get("currency", "INR"), "description": body.get("description"),
             "incurred_on": body.get("incurred_on"), "budget": body.get("budget"), "project": body.get("project"),
             "status": "pending", "scope": ({"org_unit": me.get("org_unit")} if me.get("org_unit") else None),
             "created_at": now}
    ctx["kwrite"](eid, claim)
    routed = None
    if mgr_login:
        iid = f"ds:{tenant}:platform/inbox_item/expense-{eid.rsplit('/', 1)[-1]}"
        ctx["kwrite"](iid, {"schema": "platform.inbox_item", "id": iid, "tenant": tenant, "actor": mgr_login,
            "title": f"Expense claim — {me.get('name') or me['id']}",
            "body": f"{claim.get('category') or 'expense'} · {body['amount']}", "ref": eid, "kind": "task",
            "status": "unread", "created_at": now})
        routed = mgr_login
    return {"filed": eid, "status": "pending", "routed_to": routed, "manager": (mgr or {}).get("name")}, None


def h_expense_decide(ctx):
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    eid, decision = body.get("expense_id"), body.get("decision")
    if decision not in ("approve", "reject"):
        return None, "decision must be approve|reject"
    claim = ctx["kget"](eid) if eid else None
    if not claim:
        return None, "expense claim not found"
    if claim.get("manager_login") != sub:
        return None, "only the applicant's manager may decide this claim"
    now = _now(); finance_txn = None
    if decision == "approve":
        # 1) approver authority — the manager's expense approval_limit policy (via the POLICY ENGINE)
        pres, perr = ctx["call"](POLICY_ENG, "policy-resolve", {"domain": "expense"})
        pols = (pres or {}).get("policies", [])
        limit = next(((p.get("rules") or {}).get("approval_limit") for p in pols
                      if (p.get("rules") or {}).get("approval_limit") is not None), None)
        if limit is not None and claim.get("amount", 0) > limit:
            return None, f"amount {claim.get('amount')} exceeds your approval limit of {limit} — needs higher approval"
        # 2) settle into the ledger via the FINANCE ENGINE (Dr expense / Cr Payables, committed to the budget)
        budget = body.get("budget") or claim.get("budget")
        if not budget:
            return None, "select a budget to charge this expense to"
        fr, ferr = ctx["call"](FINANCE_ENG, "finance-post", {"direction": "expenditure", "amount": claim["amount"],
            "budget": budget, "contra_type": "liability", "status": "committed", "source": eid, "source_kind": "hr.expense_claim",
            "project": claim.get("project"), "org_unit": (claim.get("scope") or {}).get("org_unit"),
            "memo": (claim.get("description") or claim.get("category") or "Expense") + " — " + (claim.get("applicant_login", "").rsplit("/", 1)[-1])})
        if ferr:
            return None, "settlement blocked: " + ferr
        finance_txn = fr["posted"]
        ctx["kwrite"](eid, {**claim, "status": "approved", "decided_by": sub, "decided_at": now, "budget": budget, "finance_txn": finance_txn})
    else:
        ctx["kwrite"](eid, {**claim, "status": "rejected", "decided_by": sub, "decided_at": now})
    if claim.get("applicant_login"):
        nid = f"ds:{tenant}:platform/inbox_item/expres-{eid.rsplit('/', 1)[-1]}"
        ctx["kwrite"](nid, {"schema": "platform.inbox_item", "id": nid, "tenant": tenant, "actor": claim["applicant_login"],
            "title": f"Expense {'approved' if decision == 'approve' else claim.get('status')}",
            "body": f"{claim.get('category') or 'expense'} {claim.get('amount')}", "ref": eid, "kind": "notification",
            "status": "unread", "created_at": now})
    iid = f"ds:{tenant}:platform/inbox_item/expense-{eid.rsplit('/', 1)[-1]}"
    prior = ctx["kget"](iid)
    if prior:
        ctx["kwrite"](iid, {**prior, "status": "done"})
    return {"decided": decision, "status": ("approved" if decision == "approve" else "rejected"), "finance_txn": finance_txn}, None


HANDLERS = {
    "leave-apply": h_leave_apply,
    "leave-decide": h_leave_decide,
    "attendance-mark": h_attendance_mark,
    "expense-apply": h_expense_apply,
    "expense-decide": h_expense_decide,
}


def main():
    print("HR Workflows Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
