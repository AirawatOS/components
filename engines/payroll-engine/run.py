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
PAYROLL_ENGINE_ID = "ds:airawatdeveloper:engine/payroll-engine"
_ADMIN = {"platform.tenant-admin", "platform.steward", "admin"}


def _proration(period, from_date, to_date):
    """Fraction of the month `period` (YYYY-MM) that the allocation [from_date, to_date] actually covers, at DAY
    granularity — so a mid-month JOINER (from_date inside the month) or LEAVER (to_date inside the month) is charged
    pro-rata, while an allocation spanning the whole month gets factor 1.0. Returns (factor, active_days, days_in_month)."""
    import calendar
    y, m = int(period[:4]), int(period[5:7])
    dim = calendar.monthrange(y, m)[1]
    start, end = 1, dim
    if from_date:
        fm = from_date[:7]
        if fm > period:
            return 0.0, 0, dim                              # starts after this month
        if fm == period:
            try: start = min(max(1, int(from_date[8:10])), dim)
            except Exception: pass
    if to_date:
        tm = to_date[:7]
        if tm < period:
            return 0.0, 0, dim                              # ended before this month
        if tm == period:
            try: end = min(max(1, int(to_date[8:10])), dim)
            except Exception: pass
    active = max(0, end - start + 1)
    return round(active / dim, 6), active, dim


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
        factor, active_days, dim = _proration(period, a.get("from_date"), a.get("to_date"))
        full = round((comp.get("monthly_cost") or 0) * (a.get("percent") or 0) / 100.0, 2)
        cost = round(full * factor, 2)                     # pro-rata for a mid-month joiner/leaver (factor 1.0 = full month)
        if cost <= 0:
            skipped.append({**base, "reason": "no active days in this month"}); continue
        tid = f"ds:{ctx['tenant']}:finance/transaction/payroll-{local}-{period}"
        lines.append({**base, "monthly_cost": comp.get("monthly_cost"), "full_cost": full, "cost": cost,
                      "proration": factor, "active_days": active_days, "days_in_month": dim, "prorated": factor < 1.0,
                      "budget": bud.get("id"), "org_unit": emp.get("org_unit") or bud.get("org_unit"),
                      "txn_id": tid, "date": period + "-28"})
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
        pr = f" · {l['active_days']}/{l['days_in_month']}d pro-rata" if l.get("prorated") else ""
        res, err = ctx["call"](FINANCE_ENG, "finance-post", {
            "id": l["txn_id"], "direction": "expenditure", "amount": l["cost"], "budget": l["budget"],
            "project": l["project"], "org_unit": l["org_unit"], "contra_type": "liability", "date": l["date"],
            "memo": f"Salary apportionment {period} — {l.get('employee_name') or l['employee']} @ {l['percent']}%{pr}",
            "source": l["allocation"], "source_kind": "payroll", "status": "posted", "allow_overspend": True})
        if err:
            errors.append({**l, "error": err})
        elif res and res.get("duplicate"):
            already += 1
        else:
            posted.append({"txn": res.get("posted"), "project": l["project"], "employee": l["employee"], "cost": l["cost"]})
    return {"period": period, "posted": posted, "posted_count": len(posted), "posted_total": round(sum(p["cost"] for p in posted), 2),
            "already_posted": already, "skipped": skipped, "errors": errors}, None


# --- SCHEDULE TOGGLE: manage the automatic monthly run (a platform.schedule record) -------------------------
_SCHED_SUFFIX = "platform/schedule/payroll-monthly"


def _day_from_cron(cron):
    try:
        return int((cron or "").split()[2])
    except Exception:
        return None


def _sched_state(rec):
    rec = rec or {}
    return {"exists": bool(rec.get("id")), "enabled": bool(rec.get("enabled")),
            "day": _day_from_cron(rec.get("cron")) or 28, "cron": rec.get("cron"),
            "next_run": rec.get("next_run"), "last_run": rec.get("last_run"), "last_status": rec.get("last_status")}


def h_payroll_schedule(ctx):
    """get → the current automatic-run state; set {enabled, day} → upsert the platform.schedule + (on enable) ensure
    the per-tenant install-as-grant so the scheduler can actually fire it. Admin-gated for set."""
    body, tenant = ctx["body"], ctx["tenant"]
    sid = f"ds:{tenant}:{_SCHED_SUFFIX}"
    cur = ctx["kget"](sid) or {}
    if body.get("action", "get") == "get":
        return _sched_state(cur), None
    if not (set(ctx["claims"].get("roles", [])) & _ADMIN):
        return None, "managing the payroll schedule requires a tenant admin"
    enabled = bool(body.get("enabled"))
    day = int(body.get("day") or _day_from_cron(cur.get("cron")) or 28)
    day = max(1, min(28, day))                              # 1–28 so it exists in every month (5-field cron has no last-day)
    rec = {**cur, "schema": "platform.schedule", "id": sid, "tenant": tenant,
           "name": "Monthly payroll apportionment", "component": PAYROLL_ENGINE_ID,
           "cron": f"0 2 {day} * *", "enabled": enabled, "overlap": "skip"}
    ctx["kwrite"](sid, rec)
    install = None
    if enabled and ctx.get("ensure_install"):              # mint the scheduler's run-token (idempotent) so it can fire
        install = ctx["ensure_install"](PAYROLL_ENGINE_ID)
    st = _sched_state(rec)
    st["install"] = ("error: " + install["error"]) if (install and install.get("error")) else ("ready" if install else None)
    return st, None


HANDLERS = {"payroll-preview": h_payroll_preview, "payroll-apportion": h_payroll_apportion,
            "payroll-schedule": h_payroll_schedule}


# --- HEADLESS / SCHEDULED RUN (the monthly cron path) --------------------------------------------------------
# The platform scheduler fires this engine once a month via the app-host launcher (confined, its OWN install
# run-token). Here we build a broker-backed ctx (the /v1/query + /v1/write contract the launcher injects
# AIRAWAT_API + RUN_TOKEN for) and run the SAME h_payroll_apportion. ctx["call"] loads the finance-engine's
# code in-process and runs its handler over the shared store — so the double-entry logic is reused, and writes
# go through THIS run-token (so the scheduled path needs payroll-engine's own finance write grants).
def _headless_ctx(period):
    import os, json, urllib.request, importlib.util
    API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
    TOKEN = os.environ.get("RUN_TOKEN", "")
    tenant = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""

    def bcall(path, body):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=60).read() or b"{}")

    store = {}                                             # id → object, preloaded per scope (read-free kget/kfind + write-through)
    for scope in ("project.allocation", "org.employee", "hr.compensation", "finance.budget", "finance.transaction", "finance.account"):
        for r in bcall("/v1/query", {"scope": scope, "limit": 10000}).get("results", []):
            o = r.get("object") or r
            if o.get("id"):
                store[o["id"]] = o

    def kfind(schema, where=None):
        out = [o for o in store.values() if o.get("schema") == schema]
        return [o for o in out if all(o.get(k) == v for k, v in where.items())] if where else out

    def kget(oid):
        return store.get(oid)

    def kwrite(oid, obj):
        bcall("/v1/write", {"scope": obj.get("schema"), "id": oid, "object": obj}); store[oid] = obj; return {"id": oid}

    _mods = {}
    def call(eng, verb, body):
        if eng not in _mods:                               # load the sibling engine's code once (in-process composition)
            d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", eng.rsplit("/", 1)[-1])
            spec = importlib.util.spec_from_file_location("hl_" + eng.rsplit("/", 1)[-1], os.path.join(d, "run.py"))
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); _mods[eng] = m
        return _mods[eng].HANDLERS[verb]({**ctx, "body": body})

    ctx = {"tenant": tenant, "sub": f"service://{tenant}/payroll-scheduler",
           "claims": {"tenant": tenant, "sub": f"service://{tenant}/payroll-scheduler", "roles": ["platform.scheduler"]},
           "body": {"period": period}, "kfind": kfind, "kget": kget, "kwrite": kwrite, "call": call}
    return ctx


def main():
    import os, time as _t
    # A REAL scheduled run targets an operating tenant (the scheduler injects WRITE_TENANT/TENANT — _headless_ctx
    # needs it to write). A dev Test / sanity check has neither RUN_TOKEN nor a target tenant → self-test only, so
    # it goes green without touching the ledger (the apportion needs real data the dev sandbox doesn't have).
    tenant = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT")
    if not os.environ.get("RUN_TOKEN") or not tenant:
        print("Payroll Engine — handlers:", ", ".join(sorted(HANDLERS)), "· self-test (no target tenant)")
        return 0
    period = (os.environ.get("PAYROLL_PERIOD") or _t.strftime("%Y-%m", _t.gmtime()))[:7]
    res, err = h_payroll_apportion(_headless_ctx(period))
    if err:
        print(f"[payroll] {period} FAILED: {err}"); return 1
    print(f"[payroll] {period}: posted {res['posted_count']} (total {res['posted_total']}), "
          f"already {res['already_posted']}, skipped {len(res['skipped'])}, errors {len(res['errors'])}")
    for e in res["errors"]:
        print("   error:", e.get("employee"), e.get("error"))
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
