#!/usr/bin/env python3
"""Policy Engine — resolve the active org.policy records for an employee × domain, most-specific first.

Resolves the employee's CLASSIFICATION context (seniority rank via posting→position→level, stream, role, unit) and
matches each policy's `applies_to`. Reads the org tree with THIS engine's OWN read grants (component-attributed) —
no elevated tenant-admin token. Handler: policy-resolve. Also callable by other engines via ctx["call"].
Returns ({domain, employee, context, policies}, None).
"""
_ADMIN = {"platform.tenant-admin", "platform.steward", "admin"}


def h_policy_resolve(ctx):
    body, claims = ctx["body"], ctx["claims"]
    domain = body.get("domain")
    if not domain:
        return None, "domain required"
    # PRIVACY: only an admin (e.g. the Policy Console preview) may resolve for ANOTHER employee; staff resolve self.
    is_admin = bool(set(claims.get("roles", [])) & _ADMIN)
    target = body.get("employee") if is_admin else None
    emps = ctx["kfind"]("org.employee")
    emp = next((e for e in emps if e.get("id") == target), None) if target \
        else next((e for e in emps if e.get("citizen") == ctx["sub"]), None)
    cx = {"rank": None, "stream": None, "roles": [], "org_unit": (emp or {}).get("org_unit")}
    if emp:
        postings = [p for p in ctx["kfind"]("org.posting") if p.get("employee") == emp.get("id") and p.get("status") != "ended"]
        pos_by_id = {p["id"]: p for p in ctx["kfind"]("org.position")}
        lvl_by_code = {l.get("code"): l for l in ctx["kfind"]("hr.job_level")}
        for pg in postings:
            if pg.get("role") and pg["role"] not in cx["roles"]:
                cx["roles"].append(pg["role"])
            pos = pos_by_id.get(pg.get("position") or "")
            if pos:
                cx["org_unit"] = cx["org_unit"] or pos.get("org_unit")
                if pos.get("stream"):
                    cx["stream"] = pos.get("stream")
                lv = lvl_by_code.get(pos.get("level"))
                if lv and isinstance(lv.get("rank"), (int, float)):
                    cx["rank"] = max(cx["rank"] or 0, lv["rank"])

    def matches(a):
        a = a or {}
        if a.get("min_rank") is not None and (cx["rank"] is None or cx["rank"] < a["min_rank"]): return False
        if a.get("max_rank") is not None and (cx["rank"] is None or cx["rank"] > a["max_rank"]): return False
        if a.get("stream") and a["stream"] != cx["stream"]: return False
        if a.get("role") and a["role"] not in cx["roles"]: return False
        if a.get("org_unit") and a["org_unit"] != cx["org_unit"]: return False
        return True

    def specificity(p):
        a = p.get("applies_to") or {}
        return (sum(1 for k in ("min_rank", "max_rank", "stream", "role", "org_unit", "jurisdiction") if a.get(k) is not None),
                p.get("priority") or 0, p.get("created_at") or "")

    pols = [p for p in ctx["kfind"]("org.policy") if p.get("domain") == domain and p.get("status", "active") == "active" and matches(p.get("applies_to"))]
    pols.sort(key=specificity, reverse=True)
    return {"domain": domain, "employee": (emp or {}).get("id"), "context": cx, "policies": pols}, None


HANDLERS = {"policy-resolve": h_policy_resolve}


def main():
    print("Policy Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
