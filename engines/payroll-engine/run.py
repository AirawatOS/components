#!/usr/bin/env python3
"""Payroll Engine — apportion LABOUR cost to projects (the salaries→projects wiring).

For a period, each project.allocation (employee → project at %) becomes a project-tagged salary transaction:
    cost = hr.compensation.monthly_cost × (allocation.percent / 100)
posted via the finance-engine (ctx.call → finance-post) so it rolls into the project's budget + shows up in the
project rollup's `spent`, exactly like expense/travel. NO fabrication: an allocated employee with no compensation
record, or an allocation whose project has no budget, is REPORTED as skipped — never guessed. Idempotent per
(allocation, period) via a deterministic transaction id (a re-run does not double-charge).

Handlers: payroll-preview (dry-run: what would post) · payroll-apportion (post). Composes finance-engine.
"""
import time as _t

FINANCE_ENG = "ds:airawatdeveloper:engine/finance-engine"


def _current_comp(ctx, employee, period):
    """Latest ACTIVE hr.compensation for the employee effective on/before the period (YYYY-MM). None if absent."""
    rows = [c for c in ctx["kfind"]("hr.compensation", {"employee": employee})
            if c.get("status") != "ended" and (c.get("monthly_cost") is not None)
            and (not c.get("effective_from") or c["effective_from"][:7] <= period)]
    rows.sort(key=lambda c: c.get("effective_from") or "", reverse=True)
    return rows[0] if rows else None


def _plan(ctx, period, only_project):
    """Build the apportionment plan for the period: one line per active allocation, with skip reasons captured."""
    now = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
    budgets = {b.get("project"): b for b in ctx["kfind"]("finance.budget") if b.get("project")}
    lines, skipped = [], []
    for a in ctx["kfind"]("project.allocation"):
        if a.get("status") == "ended":
            continue
        proj = a.get("project")
        if only_project and proj != only_project:
            continue
        # allocation active in this period? (respect from/to if set)
        if a.get("from_date") and a["from_date"][:7] > period:
            continue
        if a.get("to_date") and a["to_date"][:7] < period:
            continue
        emp = ctx["kget"](a.get("employee") or "") or {}
        comp = _current_comp(ctx, a.get("employee"), period)
        bud = budgets.get(proj)
        local = (a.get("id") or "").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        base = {"allocation": a.get("id"), "employee": a.get("employee"), "employee_name": emp.get("name"),
                "project": proj, "percent": a.get("percent")}
        if comp is None:
            skipped.append({**base, "reason": "no compensation record for this employee"}); continue
        if not bud:
            skipped.append({**base, "reason": "project has no committed budget (not approved?)"}); continue
        cost = round((comp.get("monthly_cost") or 0) * (a.get("percent") or 0) / 100.0, 2)
        if cost <= 0:
            skipped.append({**base, "reason": "computed cost is zero"}); continue
        tid = f"ds:{ctx['tenant']}:finance/transaction/payroll-{local}-{period}"
        lines.append({**base, "monthly_cost": comp.get("monthly_cost"), "cost": cost, "budget": bud.get("id"),
                      "org_unit": emp.get("org_unit") or bud.get("org_unit"), "txn_id": tid, "date": period + "-28"})
    return lines, skipped, now


def h_payroll_preview(ctx):
    period = ctx["body"].get("period")
    if not period or len(period) < 7:
        return None, "period (YYYY-MM) required"
    lines, skipped, _ = _plan(ctx, period[:7], ctx["body"].get("project"))
    total = round(sum(l["cost"] for l in lines), 2)
    return {"period": period[:7], "lines": lines, "skipped": skipped, "total": total, "count": len(lines)}, None


def h_payroll_apportion(ctx):
    period = ctx["body"].get("period")
    if not period or len(period) < 7:
        return None, "period (YYYY-MM) required"
    period = period[:7]
    lines, skipped, _ = _plan(ctx, period, ctx["body"].get("project"))
    posted, already, errors = [], 0, []
    for l in lines:
        if ctx["kget"](l["txn_id"]):                          # idempotent: already apportioned this (allocation, period)
            already += 1; continue
        res, err = ctx["call"](FINANCE_ENG, "finance-post", {
            "id": l["txn_id"], "direction": "expenditure", "amount": l["cost"], "budget": l["budget"],
            "project": l["project"], "org_unit": l["org_unit"], "contra_type": "liability", "date": l["date"],
            "memo": f"Salary apportionment {period} — {l.get('employee_name') or l['employee']} @ {l['percent']}%",
            "source": l["allocation"], "source_kind": "payroll", "status": "posted", "allow_overspend": True})
        if err:
            errors.append({**l, "error": err})
        elif res and res.get("duplicate"):
            already += 1
        else:
            posted.append({"txn": res.get("posted"), "project": l["project"], "employee": l["employee"], "cost": l["cost"]})
    return {"period": period, "posted": posted, "posted_count": len(posted), "posted_total": round(sum(p["cost"] for p in posted), 2),
            "already_posted": already, "skipped": skipped, "errors": errors}, None


HANDLERS = {"payroll-preview": h_payroll_preview, "payroll-apportion": h_payroll_apportion}


def main():
    print("Payroll Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
