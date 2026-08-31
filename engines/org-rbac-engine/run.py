#!/usr/bin/env python3
"""Org RBAC Engine — the org-RBAC AUTHORITY. Three handlers, all attributed to this engine + gated by its grants:

  posting-apply      project an org.posting → a live governance.role_grant (a person's effective role in a scope).
  authz-check        the scope-aware POLICY DECISION POINT: "may the signed-in user act as <role> on <scope>?"
  employee-provision mint a tenant LOGIN for a staff member (the identity SECRET stays host-side; exposed to the
                     engine only as the `ctx["provision_login"]` capability — the engine holds the LOGIC, not the key).

SCOPE model: a role_grant may carry {org_unit, area, jurisdiction}; it holds the role only WHERE that node is an
ancestor-or-self of the target's node. Containment is READ-FREE at decision time — a scoped WRITER stamps an item's
scope with ancestor `*_path` arrays, so `grant_covers` is pure set-membership. `grant_covers`/`scope_has_paths` are
module-level so the HOST inbox/task filter imports them (single source of truth for containment) — see app_host.py.
"""
import time as _t

_ADMIN = {"platform.tenant-admin", "platform.steward", "admin"}


# --- posting → role_grant -------------------------------------------------------------------------------------
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


# --- scope containment (module-level: the HOST inbox/task filter imports these) --------------------------------
def scope_has_paths(scope):
    """True if `scope` carries any materialised ancestor path (so a read-free containment check is possible)."""
    return bool(scope) and any(scope.get(d + "_path") for d in ("org_unit", "area", "jurisdiction"))


def grant_covers(grant_scope, item_scope):
    """READ-FREE, ITEM-DRIVEN: does a grant COVER an item? For each AXIS the item localises on (via its materialised
    `*_path`), if the grant constrains that axis the item's node must be within the grant's subtree (grant ∈ path).
    Axes the item omits don't exclude it; axes the grant omits are tenant-wide. The item must overlap ≥1 constrained
    axis (else a scoped grant would vacuously cover work it has no authority over). An UNSCOPED grant covers all."""
    if not grant_scope:
        return True
    item_scope = item_scope or {}
    constrained = [d for d in ("org_unit", "area", "jurisdiction") if grant_scope.get(d)]
    overlap = False
    for dim in constrained:
        path = item_scope.get(dim + "_path")
        if path:
            overlap = True
            if grant_scope[dim] not in path:
                return False
    return overlap


# --- materialisation (for authz-check TARGETS — walks the org tree via THIS engine's read grants) -------------
def _node_ancestors(ctx, node_id, cache):
    if node_id in cache:
        return cache[node_id]
    chain, seen, cur = [], set(), node_id
    while cur and cur not in seen:
        seen.add(cur); chain.append(cur)
        cur = (ctx["kget"](cur) or {}).get("parent")
    cache[node_id] = chain
    return chain


def _juris_ancestors(ctx, code):
    try:
        return ctx["http"]("POST", ctx["network_url"] + "/v1/jurisdictions/resolve", {"code": code}).get("ancestors", [code])
    except Exception:
        return [code]


def _materialize(ctx, scope):
    if not scope:
        return {}
    out, c = dict(scope), {}
    if scope.get("org_unit") and not scope.get("org_unit_path"):         out["org_unit_path"] = _node_ancestors(ctx, scope["org_unit"], c)
    if scope.get("area") and not scope.get("area_path"):                 out["area_path"] = _node_ancestors(ctx, scope["area"], c)
    if scope.get("jurisdiction") and not scope.get("jurisdiction_path"): out["jurisdiction_path"] = _juris_ancestors(ctx, scope["jurisdiction"])
    return out


def _effective_grants(ctx):
    """The caller's role holdings as records: identity-token roles (tenant-wide, scope=None) UNION non-revoked
    governance.role_grant records (scope + source). Read via THIS engine's own role_grant read grant."""
    claims = ctx["claims"]
    out = [{"role": r, "scope": None, "via": "identity"} for r in claims.get("roles", [])]
    for g in ctx["kfind"]("governance.role_grant", {"subject": claims.get("sub")}):
        if g.get("tenant") == claims.get("tenant") and g.get("role") and not g.get("revoked"):
            out.append({"role": g["role"], "scope": g.get("scope") or None, "via": "grant", "source": g.get("source")})
    return out


# --- authz-check: the scope-aware PDP -------------------------------------------------------------------------
def h_authz_check(ctx):
    body = ctx["body"]
    role = body.get("role")
    if not role:
        return None, "role required"
    tgt = body.get("scope") or {}
    mat = _materialize(ctx, tgt) if tgt else {}
    for g in _effective_grants(ctx):
        if g["role"] == role and grant_covers(g.get("scope"), mat):
            return {"allowed": True, "role": role, "scope": tgt, "via": g.get("via"),
                    "grant_scope": g.get("scope"), "source": g.get("source")}, None
    return {"allowed": False, "role": role, "scope": tgt, "via": None, "grant_scope": None}, None


# --- employee-provision: LOGIC here; the identity SECRET stays host-side (ctx["provision_login"]) --------------
def h_employee_provision(ctx):
    claims, tenant, body = ctx["claims"], ctx["tenant"], ctx["body"]
    if not (set(claims.get("roles", [])) & _ADMIN):
        return None, "provisioning a login requires a tenant admin"
    emp = body.get("employee") or {}
    login = body.get("username") or emp.get("citizen") or ""
    uname = login.replace("user://%s/" % tenant, "").rsplit("/", 1)[-1] if login else ""
    if not uname:
        return None, "no login username (set the employee's Login user)"
    try:                                                       # account with roles=[]: a staff login; POSTINGS grant roles
        r = ctx["provision_login"](uname, tenant, [])
    except Exception as e:
        return None, "provisioning failed: " + str(e)[:150]
    return {"provisioned": r.get("provisioned", uname), "subject": r.get("subject"),
            "existed": r.get("existed", False), "tenant": tenant}, None


HANDLERS = {"posting-apply": h_posting_apply, "authz-check": h_authz_check, "employee-provision": h_employee_provision}


def main():
    print("Org RBAC Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
