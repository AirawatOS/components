#!/usr/bin/env python3
"""Lifecycle Engine — ADR-0091 V3 (Lifecycle plane). ONE generic engine that drives ANY governed
`platform.lifecycle` state-machine declaration: request → open a case at the start state (guarded, routed),
decide → apply a named transition (legal-from-state + authorized + guarded → decided change-request + effects +
routing), status → the case state + open tasks. No per-domain code: hr.leave, expense, proposal, bail all run
this engine; only their platform.lifecycle records differ.

Verbs: lifecycle-request | lifecycle-decide | lifecycle-status  (namespaced to avoid colliding with the existing
/bridge/decide governance endpoint; the bare Decision/Lifecycle names converge later — see ADR-0091).

The case IS the domain record (its `status` field holds the state); transitions are recorded as attributed,
bitemporal registry writes (ADR-0056 decided change-request), so History/status replay the full lineage.
"""
import time as _t, uuid as _u, json as _json

RULE_ENGINE = "ds:airawat:engine/rule-engine"          # Decision-plane evaluator for transition guards (ADR-0088/0091 V2)


def _now():
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())


def _wf(ctx, workflow):
    return next((d for d in ctx["kfind"]("platform.lifecycle")
                 if d.get("workflow") == workflow and d.get("enabled", True)), None)


def _guard_ok(ctx, tr, facts):
    """A transition guard is a Decision-plane evaluate over a governed ruleset. Blocks only on an explicit
    deny (decision.allow == False); no ruleset / no match ⇒ allowed. Returns (ok, reason)."""
    g = tr.get("guard") or {}
    rs = g.get("ruleset")
    if not rs:
        return True, None
    try:
        res, err = ctx["call"](RULE_ENGINE, "evaluate", {"ruleset": rs, "facts": facts, "mode": g.get("mode") or "first"})
        dec = (res or {}).get("decision") if not err else None
        if isinstance(dec, dict) and dec.get("allow") is False:
            return False, dec.get("reason") or f"blocked by {rs}"
    except Exception as e:
        return False, "guard error: " + str(e)[:100]
    return True, None


def _actions_for(ctx, decl, state, workflow, case):
    """Synthesize Inbox actions for the tasks available FROM a state — each onward transition becomes a button
    that calls lifecycle-decide with the right transition. Keeps the Inbox generic across every workflow."""
    acts = []
    for t in decl.get("transitions", []):
        if t.get("from") == state and (t.get("role") or t.get("actor_field")):
            # platform.inbox_item.actions items are JSON strings (matches the bespoke Inbox convention)
            acts.append(_json.dumps({"label": (t.get("label") or t["name"].title()),
                                     "verb": "lifecycle-decide", "style": t.get("style") or "pri",
                                     "args": {"workflow": workflow, "case": case, "transition": t["name"]}}))
    return acts


def h_request(ctx):
    """Open a new case: apply the start transition (guarded), write the domain record at its first state, route
    the first task to the approver named by the transition's `route.to_field`."""
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    workflow = body.get("workflow"); payload = dict(body.get("payload") or {})
    if not workflow:
        return None, "workflow required"
    decl = _wf(ctx, workflow)
    if not decl:
        return None, f"no platform.lifecycle for '{workflow}'"
    start = decl.get("start")
    tr = next((t for t in decl.get("transitions", []) if t.get("from") == start), None)
    if not tr:
        return None, f"no start transition from '{start}'"
    # PREPARE (ADR-0091 V3): a declared context provider enriches the payload with domain-derived fields
    # (e.g. resolve the caller's employee + manager) so the generic engine builds the record from a bare body.
    prep = decl.get("prepare") or {}
    if prep.get("engine") and prep.get("verb") and ctx.get("call"):
        try:
            pres, perr = ctx["call"](prep["engine"], prep["verb"], dict(payload))
            if not perr and isinstance(pres, dict):
                for k, v in pres.items():
                    payload.setdefault(k, v)                  # provider fills gaps; explicit body wins
        except Exception as e:
            return None, "prepare failed: " + str(e)[:120]
    ok, reason = _guard_ok(ctx, tr, payload)
    if not ok:
        return None, reason
    now = _now(); cid = f"ds:{tenant}:{decl['record_schema'].replace('.', '/')}/{_u.uuid4().hex[:8]}"
    rec = {**payload, "schema": decl["record_schema"], "id": cid, "tenant": tenant,
           "status": tr["to"], "applicant_login": payload.get("applicant_login") or sub, "created_at": now}
    route = tr.get("route") or {}
    approver = rec.get(route.get("to_field")) if route.get("to_field") else None
    if approver:
        iid = f"ds:{tenant}:platform/inbox_item/{workflow.replace('.', '-')}-{cid.rsplit('/', 1)[-1]}"
        ctx["kwrite"](iid, {"schema": "platform.inbox_item", "id": iid, "tenant": tenant, "actor": approver,
                            "title": route.get("title") or f"{workflow}: action required", "ref": cid,
                            "kind": "task", "status": "unread", "created_at": now,
                            "actions": _actions_for(ctx, decl, tr["to"], workflow, cid)})
    ctx["kwrite"](cid, rec)
    return {"case": cid, "workflow": workflow, "status": rec["status"], "routed_to": approver}, None


def h_decide(ctx):
    """Apply a named transition to an existing case: must be legal from the current state, authorized
    (actor_field match and/or role), and pass its guard; then record the decided change (state + decided_by/at),
    fire effects, close the task, and notify."""
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    workflow, case, tname = body.get("workflow"), body.get("case"), body.get("transition")
    if not (workflow and case and tname):
        return None, "workflow + case + transition required"
    decl = _wf(ctx, workflow)
    if not decl:
        return None, f"no platform.lifecycle for '{workflow}'"
    rec = ctx["kget"](case)
    if not rec:
        return None, "case not found"
    cur = rec.get("status")
    tr = next((t for t in decl.get("transitions", []) if t.get("name") == tname), None)
    if not tr:
        return None, f"unknown transition '{tname}'"
    if tr.get("from") != cur:
        return None, f"illegal transition '{tname}' from state '{cur}' (needs '{tr.get('from')}')"
    # authorization: actor_field (e.g. the record's manager_login must equal the caller) and/or a role claim
    if tr.get("actor_field") and rec.get(tr["actor_field"]) != sub:
        return None, f"only {tr['actor_field']} may apply '{tname}'"
    if tr.get("role") and tr["role"] not in (ctx["claims"].get("roles", []) or []):
        return None, f"role '{tr['role']}' required for '{tname}'"
    ok, reason = _guard_ok(ctx, tr, dict(rec, **(body.get("payload") or {})))
    if not ok:
        return None, reason
    now = _now()
    # write only conventional lifecycle fields the domain record schemas already carry (status/decided_by/decided_at);
    # richer case metadata (transition name, history) belongs on a separate case record — a follow-up, not the
    # domain record (which is additionalProperties:false).
    updated = {**rec, "status": tr["to"], "decided_by": sub, "decided_at": now}
    # effects: {"write": <record template>} — additive, generic (composing engines via {"call":...} is future work)
    for eff in (tr.get("effects") or []):
        if isinstance(eff, dict) and eff.get("write"):
            w = dict(eff["write"]); wid = w.get("id") or f"ds:{tenant}:{w.get('schema','platform/note').replace('.', '/')}/{_u.uuid4().hex[:8]}"
            w.update({"id": wid, "tenant": tenant, "ref": case, "created_at": now})
            ctx["kwrite"](wid, w)
    ctx["kwrite"](case, updated)
    # close the open task for this case + notify (generic Inbox hygiene; task found by ref, not stored on the record)
    for t in ctx["kfind"]("platform.inbox_item"):
        if t.get("ref") == case and t.get("kind") == "task" and t.get("status") != "done":
            ctx["kwrite"](t["id"], {**t, "status": "done"})
    notify = tr.get("notify") or {}
    who = rec.get(notify.get("to_field")) if notify.get("to_field") else None
    if who:
        nid = f"ds:{tenant}:platform/inbox_item/{workflow.replace('.', '-')}res-{case.rsplit('/', 1)[-1]}"
        ctx["kwrite"](nid, {"schema": "platform.inbox_item", "id": nid, "tenant": tenant, "actor": who,
                            "title": f"{workflow}: {tr['to']}", "ref": case, "kind": "notification",
                            "status": "unread", "created_at": now})
    return {"case": case, "workflow": workflow, "transition": tname, "status": tr["to"]}, None


def h_status(ctx):
    """Read a case's current state + its open tasks (a Data-plane read projection)."""
    case = ctx["body"].get("case")
    if not case:
        return None, "case required"
    rec = ctx["kget"](case)
    if not rec:
        return None, "case not found"
    tasks = [i for i in ctx["kfind"]("platform.inbox_item") if i.get("ref") == case]
    return {"case": case, "status": rec.get("status"), "record": rec,
            "tasks": [{"id": t["id"], "actor": t.get("actor"), "kind": t.get("kind"), "status": t.get("status")} for t in tasks]}, None


HANDLERS = {"lifecycle-request": h_request, "lifecycle-decide": h_decide, "lifecycle-status": h_status}


def main():
    print("Lifecycle Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
