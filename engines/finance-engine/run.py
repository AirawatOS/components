#!/usr/bin/env python3
"""Finance Engine — the double-entry SETTLEMENT seam.

Posts a balanced journal entry (Σdebit = Σcredit) to finance.transaction and rolls the charge into a budget with an
availability check. Two modes: EXPLICIT `lines[]` (raw GL, must balance) or CONVENIENCE {direction, amount, budget|
account, contra_account|contra_type} → auto-builds the balanced 2-line entry (resolving the contra from the chart if
given a `contra_type` like "liability"). Writes with THIS engine's grants (component://finance-engine obo user).
Handler: finance-post. Returns ({posted, amount, balanced, status, budget, committed}, None) or (None, "error").
"""
import time as _t, uuid as _u


def h_finance_post(ctx):
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    now = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()); date = body.get("date") or now[:10]
    lines = body.get("lines"); budget_id = body.get("budget"); commit_amt = 0; direction = body.get("direction")
    if not lines:                                            # CONVENIENCE → auto-build a balanced 2-line entry
        amount = body.get("amount")
        if direction not in ("expenditure", "revenue") or not isinstance(amount, (int, float)) or amount <= 0:
            return None, "provide lines[], or (direction=expenditure|revenue, positive amount)"
        acct = body.get("account") or ((ctx["kget"](budget_id) or {}).get("account") if budget_id else None)
        contra = body.get("contra_account")
        if not contra and body.get("contra_type"):          # resolve the contra from the chart (e.g. first liability)
            contra = next((a["id"] for a in sorted(ctx["kfind"]("finance.account"), key=lambda z: z.get("code", ""))
                           if a.get("type") == body["contra_type"]), None)
        if not acct or not contra:
            return None, "convenience post needs an account (or a budget with an account) + a contra_account/contra_type"
        if direction == "expenditure":                      # Dr expense, Cr payable/cash
            lines = [{"account": acct, "debit": amount, "credit": 0}, {"account": contra, "debit": 0, "credit": amount}]
            commit_amt = amount
        else:                                               # revenue: Dr cash, Cr revenue
            lines = [{"account": contra, "debit": amount, "credit": 0}, {"account": acct, "debit": 0, "credit": amount}]
    dr = round(sum((l.get("debit", 0) or 0) for l in lines), 2)
    cr = round(sum((l.get("credit", 0) or 0) for l in lines), 2)
    if dr != cr:
        return None, f"unbalanced entry: debits {dr} != credits {cr}"
    status = body.get("status", "posted")
    if budget_id:                                            # budget control (availability)
        bud = ctx["kget"](budget_id)
        if not bud:
            return None, "budget not found"
        if not commit_amt and bud.get("account"):
            commit_amt = sum((l.get("debit", 0) or 0) for l in lines if l.get("account") == bud["account"])
        avail = (bud.get("allocated", 0) or 0) - (bud.get("committed", 0) or 0) - (bud.get("spent", 0) or 0)
        if commit_amt > avail and not body.get("allow_overspend"):
            return None, f"budget '{bud.get('name')}' has {avail} available; this entry needs {commit_amt}"
    tid = f"ds:{tenant}:finance/transaction/{_u.uuid4().hex[:8]}"
    ctx["kwrite"](tid, {"schema": "finance.transaction", "id": tid, "tenant": tenant, "date": date, "memo": body.get("memo"),
        "lines": lines, "amount": dr, "direction": direction, "budget": budget_id, "org_unit": body.get("org_unit"),
        "project": body.get("project"), "source": body.get("source"), "source_kind": body.get("source_kind"),
        "status": status, "posted_by": sub, "created_at": now})
    if budget_id and commit_amt:
        bud = ctx["kget"](budget_id); key = "committed" if status == "committed" else "spent"
        bud[key] = (bud.get(key, 0) or 0) + commit_amt
        ctx["kwrite"](budget_id, bud)
    return {"posted": tid, "amount": dr, "balanced": True, "status": status, "budget": budget_id, "committed": commit_amt}, None


HANDLERS = {"finance-post": h_finance_post}


def main():
    print("Finance Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
