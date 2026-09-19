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

RULE_ENGINE = "ds:airawatdeveloper:engine/rule-engine"          # Decision-plane evaluator for transition guards (ADR-0088/0091 V2)


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


def _resolve(v, vars):
    """ADR-0097 case-spine: templating. $payload.x / $record.x / $case / $sub / $tenant / $now → values from `vars`.
    Recurses into dicts/lists so an effect's `write` template can be built from the request payload + case."""
    if isinstance(v, dict):  return {k: _resolve(x, vars) for k, x in v.items()}
    if isinstance(v, list):  return [_resolve(x, vars) for x in v]
    if isinstance(v, str):
        if v == "$case":   return vars.get("case")
        if v == "$sub":    return vars.get("sub")
        if v == "$tenant": return vars.get("tenant")
        if v == "$now":    return vars.get("now")
        if v.startswith("$payload."): return (vars.get("payload") or {}).get(v[9:])
        if v.startswith("$record."):  return (vars.get("record") or {}).get(v[8:])
        if v.startswith("$subject."): return (vars.get("subject") or {}).get(v[9:])  # id of an earlier `as:`-named write in this transition
    return v


def _authorized(tr, rec, ctx):
    """Transition authorization. OR-form (ADR-0097): authorize:{any:[{actor_field},{role}]} — ANY clause passes.
    Legacy AND-form: actor_field (rec[field]==caller) AND role (claim). Returns (ok, reason)."""
    sub = ctx["sub"]; roles = ctx["claims"].get("roles", []) or []
    _fv = lambda f: rec.get(f) if rec.get(f) is not None else (rec.get("scope") or {}).get(f)  # spine: actor fields live in case.scope
    az = tr.get("authorize")
    if az and az.get("any"):
        for cl in az["any"]:
            if cl.get("actor_field") and _fv(cl["actor_field"]) == sub: return True, None
            if cl.get("role") and cl["role"] in roles: return True, None
        return False, "not authorized — needs " + " or ".join(
            (c.get("actor_field") or ("role " + str(c.get("role")))) for c in az["any"])
    if tr.get("actor_field") and _fv(tr["actor_field"]) != sub:
        return False, f"only {tr['actor_field']} may apply '{tr.get('name')}'"
    if tr.get("role") and tr["role"] not in roles:
        return False, f"role '{tr['role']}' required for '{tr.get('name')}'"
    return True, None


def _state(rec):
    """The FINE workflow state. In spine mode it lives in `current_step` (case.status is a coarse enum);
    in single-record mode it IS the record's `status`."""
    return rec.get("current_step") if rec.get("schema") == "platform.case" else rec.get("status")


def _case_status(decl, state):
    """Map a fine workflow state to platform.case's coarse status enum: terminal (no outgoing transition) → closed, else open."""
    froms = {t.get("from") for t in decl.get("transitions", [])}
    return "closed" if state not in froms else "open"


def _run_effects(ctx, effects, vars, subjects=None):
    """Apply a transition's effects with templating (ADR-0091 E1 + ADR-0097):
      {write: <tmpl>}                → write a new record (collected as a case SUBJECT if `subjects` given)
      {call: {engine,verb,args,...}} → COMPOSE another certified engine; link a result field onto vars['record']
      {update_subject: {schema,patch}} → patch the case's subject record of that schema (spine mode)."""
    tenant, case, now = vars["tenant"], vars["case"], vars["now"]
    for eff in (effects or []):
        if not isinstance(eff, dict): continue
        if eff.get("write"):
            w = _resolve(dict(eff["write"]), vars)
            _as = w.pop("as", None)                                     # optional: name this record so a later effect can link to its id via $subject.<name>
            wid = w.get("id") or f"ds:{tenant}:{str(w.get('schema','platform.note')).replace('.', '/')}/{_u.uuid4().hex[:8]}"
            w.update({"id": wid, "tenant": tenant})  # inject only id+tenant; case.subjects[] is the canonical link. created_at/ref are per-schema — the declaration adds them where the (possibly strict) schema allows
            ctx["kwrite"](wid, w)
            if _as: vars.setdefault("subject", {})[_as] = wid
            if subjects is not None: subjects.append(wid)          # platform.case.subjects[] is a list of id strings
        elif eff.get("call"):
            c = eff["call"]; _args = _resolve(dict(c.get("args") or {}), vars)
            try:
                _cres, _cerr = ctx["call"](c.get("engine"), c.get("verb"), dict(_args, case=case))
                print("[lifecycle] effect call %s @ %s: %s" % (c.get("verb"), c.get("engine"),
                      "ok" if not _cerr else "ERR " + str(_cerr)[:100]), flush=True)
                if isinstance(_cres, dict) and c.get("link_field") and _cres.get(c["link_field"]) and vars.get("record") is not None:
                    vars["record"][c.get("into", "effect_ref")] = _cres[c["link_field"]]
            except Exception as _e:
                print("[lifecycle] effect call failed: %s" % str(_e)[:120], flush=True)
        elif eff.get("update_subject"):
            us = eff["update_subject"]; subs = (vars.get("record") or {}).get("subjects") or []
            sid = None                                             # subjects are id strings — resolve the one whose record matches the target schema
            for s in subs:
                _s = s if isinstance(s, str) else (s.get("id") if isinstance(s, dict) else None)
                _sr = ctx["kget"](_s) if _s else None
                if _sr and _sr.get("schema") == us.get("schema"): sid = _s; break
            if sid:
                srec = ctx["kget"](sid)
                if srec:
                    srec.update(_resolve(us.get("patch") or {}, vars)); ctx["kwrite"](sid, srec)


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
    now = _now()
    if decl.get("record_schema") == "platform.case":         # ADR-0097: multi-record CASE spine (project/funding/justice)
        cid = f"ds:{tenant}:case/{workflow.replace('.', '-')}-{_u.uuid4().hex[:8]}"
        subjects = []                                        # start-transition effects WRITE the domain records as subjects
        _run_effects(ctx, tr.get("effects"),
                     {"payload": payload, "case": cid, "sub": sub, "tenant": tenant, "now": now, "record": None},
                     subjects=subjects)
        _cf = set()                                          # auto-denormalise the routing/auth fields the declaration uses onto the case
        for _t in decl.get("transitions", []):
            _rf = (_t.get("route") or {}).get("to_field")
            if _rf: _cf.add(_rf)
            if _t.get("actor_field"): _cf.add(_t["actor_field"])
            for _cl in (_t.get("authorize") or {}).get("any", []):
                if _cl.get("actor_field"): _cf.add(_cl["actor_field"])
        route = tr.get("route") or {}
        scope = {k: payload.get(k) for k in _cf if payload.get(k) is not None}   # platform.case is strict: routing/auth fields live in its freeform `scope`
        crec = {"schema": "platform.case", "id": cid, "tenant": tenant, "workflow": workflow,
                "status": _case_status(decl, tr["to"]), "current_step": tr["to"],
                "opened_by": sub, "opened_at": now, "updated_at": now, "subjects": subjects, "scope": scope,
                "title": route.get("title") or str(payload.get("name") or payload.get("title") or workflow)}
        approver = scope.get(route.get("to_field")) if route.get("to_field") else None
        if approver:
            iid = f"ds:{tenant}:platform/inbox_item/{workflow.replace('.', '-')}-{cid.rsplit('/', 1)[-1]}"
            ctx["kwrite"](iid, {"schema": "platform.inbox_item", "id": iid, "tenant": tenant, "actor": approver,
                                "title": route.get("title") or f"{workflow}: action required", "ref": cid,
                                "kind": "task", "status": "unread", "created_at": now,
                                "actions": _actions_for(ctx, decl, tr["to"], workflow, cid)})
        ctx["kwrite"](cid, crec)
        return {"case": cid, "workflow": workflow, "status": tr["to"], "case_status": crec["status"], "routed_to": approver,
                "subjects": subjects}, None
    cid = f"ds:{tenant}:{decl['record_schema'].replace('.', '/')}/{_u.uuid4().hex[:8]}"
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
    cur = _state(rec)                                        # fine state: current_step (spine) or status (single-record)
    tr = next((t for t in decl.get("transitions", []) if t.get("name") == tname), None)
    if not tr:
        return None, f"unknown transition '{tname}'"
    if tr.get("from") != cur:
        return None, f"illegal transition '{tname}' from state '{cur}' (needs '{tr.get('from')}')"
    okz, why = _authorized(tr, rec, ctx)                     # ADR-0097: OR-auth ({any:[...]}) or legacy actor_field+role
    if not okz:
        return None, why
    ok, reason = _guard_ok(ctx, tr, dict(rec, **(body.get("payload") or {})))
    if not ok:
        return None, reason
    now = _now()
    # `updated` is the CASE row: a platform.case in spine mode (coarse status + fine current_step), or the domain
    # record in single-record mode. Effects (write subjects / compose engines / patch subjects) run generically.
    if rec.get("schema") == "platform.case":
        updated = {**rec, "status": _case_status(decl, tr["to"]), "current_step": tr["to"], "updated_at": now}
    else:
        updated = {**rec, "status": tr["to"], "decided_by": sub, "decided_at": now}
    _run_effects(ctx, tr.get("effects"),
                 {"record": updated, "payload": body.get("payload") or {}, "case": case, "sub": sub, "tenant": tenant, "now": now})
    ctx["kwrite"](case, updated)
    # close the open task for this case + notify (generic Inbox hygiene; task found by ref, not stored on the record)
    for t in ctx["kfind"]("platform.inbox_item"):
        if t.get("ref") == case and t.get("kind") == "task" and t.get("status") != "done":
            ctx["kwrite"](t["id"], {**t, "status": "done"})
    notify = tr.get("notify") or {}
    _nf = notify.get("to_field")
    who = (rec.get(_nf) if rec.get(_nf) is not None else (rec.get("scope") or {}).get(_nf)) if _nf else None
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
    return {"case": case, "status": _state(rec), "case_status": rec.get("status"), "record": rec,
            "tasks": [{"id": t["id"], "actor": t.get("actor"), "kind": t.get("kind"), "status": t.get("status")} for t in tasks]}, None


HANDLERS = {"lifecycle-request": h_request, "lifecycle-decide": h_decide, "lifecycle-status": h_status}


def main():
    print("Lifecycle Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
