#!/usr/bin/env python3
"""Org RBAC Engine — project an org.posting into a live governance.role_grant.

A posting places a person in a unit + area with a governed ROLE; this makes that role part of the person's EFFECTIVE
roles (identity ∪ non-revoked role_grant). subject = the employee's login (org.employee.citizen = user://<tenant>/<name>);
without a login the grant would be orphaned → live:false. Idempotent per posting; ending the posting revokes it.
Handler: posting-apply. Returns ({live, grant, subject, role} | {live:false, reason}, None).
"""
import time as _t


def h_posting_apply(ctx):
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    posting = body.get("posting") or {}
    pid = posting.get("id") or body.get("posting_id")
    if not pid:
        return None, "posting id required"
    if not posting.get("employee"):
        posting = ctx["kget"](pid) or posting
    local = pid.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    gid = f"ds:{tenant}:role_grant/posting--{local}"
    role = posting.get("role")
    active = posting.get("status") != "ended" and bool(role)
    emp = ctx["kget"](posting.get("employee") or "") or {}
    subject = emp.get("citizen")
    now = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
    if active and subject:
        scope = {k: v for k, v in {"org_unit": posting.get("org_unit"), "area": posting.get("area")}.items() if v}
        ctx["kwrite"](gid, {"schema": "governance.role_grant", "id": gid, "tenant": tenant, "subject": subject,
            "role": role, "granted_by": sub, "granted_at": now, "revoked": False, "scope": scope or None, "source": pid})
        return {"live": True, "grant": gid, "subject": subject, "role": role}, None
    reason = ("set the employee's Login user to make the role live" if (active and not subject)
              else ("posting has no role" if not role else "posting ended"))
    prior = ctx["kget"](gid)
    if prior and not prior.get("revoked"):
        ctx["kwrite"](gid, {**prior, "revoked": True, "granted_at": now})
    return {"live": False, "reason": reason, "grant": (gid if prior else None)}, None


HANDLERS = {"posting-apply": h_posting_apply}


def main():
    print("Org RBAC Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
