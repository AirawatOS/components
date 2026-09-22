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


class EffectError(Exception):
    """A `required` effect ({call ...,"required":true}) failed — the transition MUST abort (no write persists)."""


def _guard_ok(ctx, tr, facts):
    """A transition guard blocks the transition when it denies. Two forms (either or both):
      guard.ruleset  → a Decision-plane `evaluate` over a governed ruleset (blocks on decision.allow == False).
      guard.compute  → {engine,verb}: call a domain engine that returns {allow, reason} — blocks on allow==False
                       or a hard error. This lets a domain enforce logic the rule-engine can't express (e.g. an
                       expense approval-limit that needs the approver's policy). Returns (ok, reason)."""
    g = tr.get("guard") or {}
    rs = g.get("ruleset")
    if rs:
        try:
            res, err = ctx["call"](RULE_ENGINE, "evaluate", {"ruleset": rs, "facts": facts, "mode": g.get("mode") or "first"})
            dec = (res or {}).get("decision") if not err else None
            if isinstance(dec, dict) and dec.get("allow") is False:
                return False, dec.get("reason") or f"blocked by {rs}"
        except Exception as e:
            return False, "guard error: " + str(e)[:100]
    comp = g.get("compute")
    if comp and comp.get("engine") and comp.get("verb") and ctx.get("call"):
        try:
            res, err = ctx["call"](comp["engine"], comp["verb"], dict(facts))
            if err:
                return False, err if isinstance(err, str) else "guard failed"
            if isinstance(res, dict) and res.get("allow") is False:
                return False, res.get("reason") or "blocked by guard"
        except Exception as e:
            return False, "guard error: " + str(e)[:100]
    return True, None


def _resolve(v, vars):
    """ADR-0097 case-spine: templating. $payload.x / $record.x / $case / $sub / $tenant / $now → values from `vars`.
    Recurses into dicts/lists so an effect's `write` template can be built from the request payload + case."""
    if isinstance(v, dict):  return {k: _resolve(x, vars) for k, x in v.items()}
    if isinstance(v, list):  return [_resolve(x, vars) for x in v]
    if isinstance(v, str):
        if v == "$case":    return vars.get("case")
        if v == "$sub":     return vars.get("sub")
        if v == "$tenant":  return vars.get("tenant")
        if v == "$now":     return vars.get("now")
        if v == "$payload": return vars.get("payload")           # whole submit form → pass through to a compute {call}
        if v == "$record":  return vars.get("record")
        if v.startswith("$payload."): return (vars.get("payload") or {}).get(v[9:])
        if v.startswith("$record."):  return (vars.get("record") or {}).get(v[8:])
        if v.startswith("$subject."): return (vars.get("subject") or {}).get(v[9:])  # id of an earlier `as:`-named write in this transition
        if v.startswith("$scope."):   return ((vars.get("record") or {}).get("scope") or {}).get(v[7:])  # a value denormalised onto the case at start (incl. named subject ids)
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


def _approver(route, scope, tenant):
    """Who receives the routed task: a user named by a scope field (route.to_field), or a static role (route.role)."""
    route = route or {}
    if route.get("to_field") and (scope or {}).get(route["to_field"]):
        return scope[route["to_field"]]
    if route.get("role"):
        return f"role://{tenant}/{route['role']}"
    return None


def _materialize_scope(ctx, scope):
    """Fill the *_path ancestor chains (walk the node's `parent` link) so the Inbox's read-free containment check
    (org-rbac-engine.grant_covers) works: a grant on any ancestor of the item's node covers it. org_unit/area only
    (jurisdiction paths need the network resolver — declare area_path explicitly for those)."""
    if not isinstance(scope, dict) or not scope:
        return scope
    out = dict(scope)
    for dim in ("org_unit", "area"):
        node = scope.get(dim)
        if node and not scope.get(dim + "_path"):
            chain, seen, cur = [], set(), node
            while cur and cur not in seen:
                seen.add(cur); chain.append(cur); cur = (ctx["kget"](cur) or {}).get("parent")
            out[dim + "_path"] = chain
    return out


def _task_scope(ctx, route, vars):
    """A routed task's MATERIALISED scope (ADR-0028 RBAC) — resolved from route.scope (e.g. {area:'$payload.ward'}),
    then ancestor-materialised. The Inbox shows a scoped task only to role-holders whose grant COVERS it."""
    sc = (route or {}).get("scope")
    return {"scope": _materialize_scope(ctx, _resolve(sc, vars))} if sc else {}


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
                if _cerr and c.get("required"):                # a REQUIRED effect (e.g. finance settlement) must succeed → abort the transition
                    raise EffectError(_cerr if isinstance(_cerr, str) else "required effect failed")
                if isinstance(_cres, dict) and c.get("link_field") and _cres.get(c["link_field"]) and vars.get("record") is not None:
                    vars["record"][c.get("into", "effect_ref")] = _cres[c["link_field"]]
                if isinstance(_cres, dict) and isinstance(c.get("link"), dict) and vars.get("record") is not None:
                    for _rk, _fk in c["link"].items():         # link: {result_key: record_key,...} — copy MANY compute results onto the record
                        if _cres.get(_rk) is not None:
                            vars["record"][_fk] = _cres[_rk]
                if isinstance(_cres, dict) and c.get("subjects") and subjects is not None:  # a compute call that produced records → add their ids as case subjects
                    for _sf in c["subjects"]:
                        if _cres.get(_sf): subjects.append(_cres[_sf])
            except EffectError:
                raise
            except Exception as _e:
                if c.get("required"):
                    raise EffectError("required effect error: " + str(_e)[:120])
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
        elif eff.get("update"):                                    # patch an EXISTING record referenced by id (not a case subject) — e.g. flip a tax.candidate
            up = eff["update"]; rid = _resolve(up.get("ref"), vars)
            rrec = ctx["kget"](rid) if rid else None
            if rrec:
                rrec.update(_resolve(up.get("patch") or {}, vars)); ctx["kwrite"](rid, rrec)


def _actions_for(ctx, decl, state, workflow, case):
    """Synthesize Inbox actions for the tasks available FROM a state — each onward transition becomes a button
    that calls lifecycle-decide with the right transition. Keeps the Inbox generic across every workflow."""
    acts = []
    for t in decl.get("transitions", []):
        if t.get("from") == state and (t.get("role") or t.get("actor_field") or (t.get("authorize") or {}).get("any")):
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
    _g = _gwf(ctx, workflow)                                  # ADR-0097 W1: a platform.workflow GRAPH wins over the legacy FSM
    if _g:
        return h_graph_request(ctx, _g)
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
        _pbody = dict(payload)
        _pbody.update(prep.get("args") or {})                  # static declared config for the compute-provider (e.g. copy_fields), keyed distinctly from form fields
        try:
            pres, perr = ctx["call"](prep["engine"], prep["verb"], _pbody)
        except Exception as e:
            return None, "prepare failed: " + str(e)[:120]
        if perr:                                              # a prepare error (e.g. no employee, balance exceeded) BLOCKS — don't write a partial record
            return None, perr if isinstance(perr, str) else "prepare failed"
        if isinstance(pres, dict):
            for _dk in (pres.pop("__drop__", None) or []):     # transient inputs prepare consumed (e.g. officer→assignee) — not persisted
                payload.pop(_dk, None)
            for k, v in pres.items():
                payload.setdefault(k, v)                       # provider fills gaps; explicit body wins
    ok, reason = _guard_ok(ctx, tr, payload)
    if not ok:
        return None, reason
    now = _now()
    if decl.get("record_schema") == "platform.case":         # ADR-0097: multi-record CASE spine (project/funding/justice)
        cid = f"ds:{tenant}:case/{workflow.replace('.', '-')}-{_u.uuid4().hex[:8]}"
        subjects = []                                        # start-transition effects WRITE the domain records as subjects
        vars = {"payload": payload, "case": cid, "sub": sub, "tenant": tenant, "now": now, "record": None}
        _run_effects(ctx, tr.get("effects"), vars, subjects=subjects)
        _cf = set()                                          # auto-denormalise the routing/auth fields the declaration uses onto the case
        for _t in decl.get("transitions", []):
            _rf = (_t.get("route") or {}).get("to_field")
            if _rf: _cf.add(_rf)
            if _t.get("actor_field"): _cf.add(_t["actor_field"])
            for _cl in (_t.get("authorize") or {}).get("any", []):
                if _cl.get("actor_field"): _cf.add(_cl["actor_field"])
        route = tr.get("route") or {}
        scope = {k: payload.get(k) for k in _cf if payload.get(k) is not None}   # platform.case is strict: routing/auth fields live in its freeform `scope`
        scope.update(vars.get("subject") or {})              # `as:`-named subject ids → reachable by later transitions via $scope.<name>
        crec = {"schema": "platform.case", "id": cid, "tenant": tenant, "workflow": workflow,
                "status": _case_status(decl, tr["to"]), "current_step": tr["to"],
                "opened_by": sub, "opened_at": now, "updated_at": now, "subjects": subjects, "scope": scope,
                "title": route.get("title") or str(payload.get("name") or payload.get("title") or workflow)}
        approver = _approver(route, scope, tenant)           # a user (route.to_field) or a static role (route.role)
        if approver:
            iid = f"ds:{tenant}:platform/inbox_item/{workflow.replace('.', '-')}-{cid.rsplit('/', 1)[-1]}"
            ctx["kwrite"](iid, {"schema": "platform.inbox_item", "id": iid, "tenant": tenant, "actor": approver,
                                "title": route.get("title") or f"{workflow}: action required", "ref": cid,
                                "kind": "task", "status": "unread", "created_at": now,
                                "actions": _actions_for(ctx, decl, tr["to"], workflow, cid), **_task_scope(ctx, route, vars)})
        ctx["kwrite"](cid, crec)
        return {"case": cid, "workflow": workflow, "status": tr["to"], "case_status": crec["status"], "routed_to": approver,
                "subjects": subjects}, None
    cid = f"ds:{tenant}:{decl['record_schema'].replace('.', '/')}/{_u.uuid4().hex[:8]}"
    rec = {**payload, "schema": decl["record_schema"], "id": cid, "tenant": tenant, "status": tr["to"], "created_at": now}
    # opener attribution is domain-specific: a declaration names the field (e.g. applicant_login / assigned_by) and
    # the engine stamps it with the caller. No hardcoded `applicant_login` (that broke strict non-hr schemas). hr.*
    # declarations also get it via their prepare compute, so this only fills a gap.
    _of = decl.get("opener_field")
    if _of and rec.get(_of) is None:
        rec[_of] = sub
    # run the start transition's effects (e.g. update a related record, {call} a compute) — same as spine mode
    try:
        _run_effects(ctx, tr.get("effects"), {"payload": payload, "record": rec, "case": cid, "sub": sub, "tenant": tenant, "now": now})
    except EffectError as _e:
        return None, str(_e)
    route = tr.get("route") or {}
    approver = _approver(route, rec, tenant)                 # user (route.to_field on the record) or static role (route.role)
    if approver:
        iid = f"ds:{tenant}:platform/inbox_item/{workflow.replace('.', '-')}-{cid.rsplit('/', 1)[-1]}"
        ctx["kwrite"](iid, {"schema": "platform.inbox_item", "id": iid, "tenant": tenant, "actor": approver,
                            "title": route.get("title") or f"{workflow}: action required", "ref": cid,
                            "kind": "task", "status": "unread", "created_at": now,
                            "actions": _actions_for(ctx, decl, tr["to"], workflow, cid),
                            **_task_scope(ctx, route, {"payload": payload, "record": rec, "case": cid, "sub": sub, "tenant": tenant, "now": now})})
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
    _g = _gwf(ctx, workflow)                                  # ADR-0097 W1: graph decide (needs `node`)
    if _g:
        return h_graph_decide(ctx, _g)
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
    vars = {"record": rec, "payload": body.get("payload") or {}, "case": case, "sub": sub, "tenant": tenant, "now": now}
    # `updated` is the CASE row: a platform.case in spine mode (coarse status + fine current_step), or the domain
    # record in single-record mode. `stamp` (templated) lets the declaration record fields the SCHEMA allows
    # (e.g. hr.leave stamps decided_by/decided_at; campaign stamps outcome/ground_truth) — the engine never guesses.
    if rec.get("schema") == "platform.case":
        updated = {**rec, "status": _case_status(decl, tr["to"]), "current_step": tr["to"], "updated_at": now}
    else:
        updated = {**rec, "status": tr["to"]}
    updated.update(_resolve(tr.get("stamp") or {}, vars))
    subj = None
    if rec.get("schema") == "platform.case":                    # records written by this transition also join the case (complete audit envelope)
        subj = updated.setdefault("subjects", rec.get("subjects") or [])
    _run_effects(ctx, tr.get("effects"), {**vars, "record": updated}, subjects=subj)
    ctx["kwrite"](case, updated)
    # close the open task for this case + notify (generic Inbox hygiene; task found by ref, not stored on the record)
    for t in ctx["kfind"]("platform.inbox_item"):
        if t.get("ref") == case and t.get("kind") == "task" and t.get("status") != "done":
            ctx["kwrite"](t["id"], {**t, "status": "done"})
    scope = updated.get("scope") or {}
    # CHAIN (ADR-0097 task lane): if this transition routes onward AND the new state has actions, hand the next
    # actor (a user via route.to_field or a static route.role) an actionable task; else notify.
    route = tr.get("route") or {}
    nxt = _approver(route, scope, tenant)
    acts = _actions_for(ctx, decl, tr["to"], workflow, case)
    routed_to = None
    if nxt and acts:
        tid = f"ds:{tenant}:platform/inbox_item/{workflow.replace('.', '-')}-{tr['to']}-{case.rsplit('/', 1)[-1]}"
        ctx["kwrite"](tid, {"schema": "platform.inbox_item", "id": tid, "tenant": tenant, "actor": nxt,
                            "title": route.get("title") or f"{workflow}: {tr['to']} — action required", "ref": case,
                            "kind": "task", "status": "unread", "created_at": now, "actions": acts,
                            **_task_scope(ctx, route, {**vars, "record": updated})})
        routed_to = nxt
    notify = tr.get("notify") or {}
    _nf = notify.get("to_field")
    who = _approver(notify, {**scope, **({_nf: rec.get(_nf)} if _nf and rec.get(_nf) is not None else {})}, tenant)
    if who and who != routed_to:
        nid = f"ds:{tenant}:platform/inbox_item/{workflow.replace('.', '-')}res-{case.rsplit('/', 1)[-1]}"
        ctx["kwrite"](nid, {"schema": "platform.inbox_item", "id": nid, "tenant": tenant, "actor": who,
                            "title": f"{workflow}: {tr['to']}", "ref": case, "kind": "notification",
                            "status": "unread", "created_at": now, **_task_scope(ctx, notify, {**vars, "record": updated})})
    return {"case": case, "workflow": workflow, "transition": tname, "status": tr["to"], "routed_to": routed_to}, None


def h_status(ctx):
    """Read a case's current state + its open tasks (a Data-plane read projection)."""
    case = ctx["body"].get("case")
    if not case:
        return None, "case required"
    rec = ctx["kget"](case)
    if not rec:
        return None, "case not found"
    if (rec.get("scope") or {}).get("_graph") is not None:   # ADR-0097 W1: a graph case → graph status projection
        _g = _gwf(ctx, rec.get("workflow"))
        if _g: return h_graph_status(ctx, _g)
    tasks = [i for i in ctx["kfind"]("platform.inbox_item") if i.get("ref") == case]
    return {"case": case, "status": _state(rec), "case_status": rec.get("status"), "record": rec,
            "tasks": [{"id": t["id"], "actor": t.get("actor"), "kind": t.get("kind"), "status": t.get("status")} for t in tasks]}, None


def h_update(ctx):
    """UPDATE (ADR-0091/0097 Lifecycle verb): patch an in-flight case's record WITHOUT a state transition —
    the 'complete/edit a task node's data' operation. Applies body.patch (or payload) to the case/domain record,
    keeps the state, re-stamps updated_at. Only the opener or a current open-task holder (or a tenant admin) may
    update. The declaration's optional `updatable:[fields]` allow-list narrows what may change; protected
    structural fields (schema/id/status/current_step/…) are never patchable."""
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    workflow, case = body.get("workflow"), body.get("case")
    patch = dict(body.get("patch") or body.get("payload") or {})
    if not (workflow and case):
        return None, "workflow + case required"
    _g = _gwf(ctx, workflow)                                  # ADR-0097 W1: graph update completes a task node
    if _g:
        return h_graph_update(ctx, _g)
    decl = _wf(ctx, workflow)
    if not decl:
        return None, f"no platform.lifecycle for '{workflow}'"
    rec = ctx["kget"](case)
    if not rec:
        return None, "case not found"
    roles = ctx["claims"].get("roles", []) or []
    opener = rec.get("opened_by") or rec.get("applicant_login") or (rec.get("scope") or {}).get("applicant_login")
    has_task = any(t.get("ref") == case and t.get("actor") == sub and t.get("status") != "done"
                   for t in ctx["kfind"]("platform.inbox_item"))
    if not (opener == sub or has_task or "platform.tenant-admin" in roles or "tenant_admin" in roles):
        return None, "only the requester or a current task-holder may update this case"
    PROTECTED = {"schema", "id", "tenant", "status", "current_step", "workflow", "opened_by", "opened_at", "subjects", "scope"}
    allow = set(decl.get("updatable") or [])
    patch = {k: v for k, v in patch.items() if k not in PROTECTED and (not allow or k in allow)}
    if not patch:
        return None, "no updatable fields in patch"
    now = _now()
    updated = {**rec, **patch}
    if rec.get("schema") == "platform.case":
        updated["updated_at"] = now
    ctx["kwrite"](case, updated)
    return {"case": case, "workflow": workflow, "status": _state(updated), "updated": sorted(patch)}, None


def h_withdraw(ctx):
    """WITHDRAW (ADR-0091/0097 Lifecycle verb): terminate a case — set a terminal 'withdrawn' state, close open
    tasks, stamp who/when. Generic across workflows (no transition need be declared); if the declaration names a
    transition to 'withdrawn' (or named 'withdraw') its authorize/stamp/effects are honored. Only the opener, a
    role authorized on that transition, or a tenant admin may withdraw."""
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    workflow, case = body.get("workflow"), body.get("case")
    if not (workflow and case):
        return None, "workflow + case required"
    decl = _wf(ctx, workflow)
    if not decl:
        return None, f"no platform.lifecycle for '{workflow}'"
    rec = ctx["kget"](case)
    if not rec:
        return None, "case not found"
    roles = ctx["claims"].get("roles", []) or []
    opener = rec.get("opened_by") or rec.get("applicant_login") or (rec.get("scope") or {}).get("applicant_login")
    wtr = next((t for t in decl.get("transitions", []) if t.get("name") == "withdraw" or t.get("to") == "withdrawn"), None)
    allowed = (opener == sub) or ("platform.tenant-admin" in roles) or ("tenant_admin" in roles)
    if not allowed and wtr:
        allowed = _authorized(wtr, rec, ctx)[0]
    if not allowed:
        return None, "only the requester (or an authorized role) may withdraw this case"
    now = _now()
    vars = {"record": rec, "payload": body.get("payload") or {}, "case": case, "sub": sub, "tenant": tenant, "now": now}
    if rec.get("schema") == "platform.case":
        updated = {**rec, "status": "closed", "current_step": "withdrawn", "updated_at": now}
    else:
        updated = {**rec, "status": "withdrawn"}
    updated.update(_resolve((wtr or {}).get("stamp") or {"withdrawn_by": "$sub", "withdrawn_at": "$now"}, vars))
    if wtr and wtr.get("effects"):
        subj = updated.setdefault("subjects", rec.get("subjects") or []) if rec.get("schema") == "platform.case" else None
        _run_effects(ctx, wtr.get("effects"), {**vars, "record": updated}, subjects=subj)
    ctx["kwrite"](case, updated)
    for t in ctx["kfind"]("platform.inbox_item"):
        if t.get("ref") == case and t.get("kind") == "task" and t.get("status") != "done":
            ctx["kwrite"](t["id"], {**t, "status": "done"})
    return {"case": case, "workflow": workflow, "status": "withdrawn"}, None


def h_service_advance(ctx):
    """ADR-0097 service-interpreter, ported OUT of app_host (`_advance_service_workflow`) into this certified engine.
    Given a just-completed task on a service CASE and the resolved offering, read the offering's DECLARED
    `fulfillment.workflow` (an ordered list of {step, role, task, issues}) and either open the NEXT step's task +
    inbox in the next role's queue, or close the case when the last step is done. All the workflow LOGIC + the
    registry writes happen here (attributed to this engine obo the completing officer — the engine IS the workflow).

    Two things a clean-room engine legitimately CANNOT do (ADR-0030: no arbitrary network) are RETURNED as
    instructions for the host transport to execute: `issue` (mint a Verifiable Credential on an issues-step — a
    /v1/credentials/issue network call) and `txn` (sync the citizen's My-Requests tracker — a network call). The
    host resolves the offering (network) and feeds it in as `body.offering`. Returns {no_workflow:true} for a
    single-step service (the host then flat-closes the case)."""
    body = ctx["body"]
    task = body.get("task") or {}
    case = body.get("case") or {}
    off = body.get("offering") or {}
    provider = case.get("tenant"); sid = case.get("service")
    wf = (off.get("fulfillment") or {}).get("workflow") or []
    if not wf:
        return {"no_workflow": True}, None
    cur = task.get("workflow_step")
    idx = next((i for i, s in enumerate(wf) if s.get("step") == cur), -1)
    num = case["id"].rsplit("-", 1)[-1]
    now = _now()
    # ISSUE instruction: if the completed step declares `issues: <type>`, hand the host a fully-formed credential
    # to mint (the engine reads the citizen's submission from the registry and packs the claims).
    issue = None
    done = wf[idx] if 0 <= idx < len(wf) else None
    if done and done.get("issues"):
        subm = ctx["kget"](f"ds:{provider}:civic/submission/{sid}-{num}") or {}
        issue = {"issuer_tenant": provider, "holder": case.get("opened_by"), "type": done["issues"],
                 "title": off.get("title") or done["issues"], "claims": (subm.get("data") or {}),
                 "subject_ref": case["id"]}
    nxt = wf[idx + 1] if 0 <= idx < len(wf) - 1 else None
    if nxt:
        role = nxt.get("role"); step = nxt.get("step")
        ntid = f"ds:{provider}:task/{sid}-{num}-{step}"
        ctx["kwrite"](ntid, {"schema": "platform.task", "id": ntid, "tenant": provider, "case": case["id"],
                             "title": nxt.get("task") or step, "verb": "review", "assignee": f"role://{provider}/{role}",
                             "status": "open", "created_at": now, "workflow_step": step, "scope": case.get("scope")})
        nibx = f"ds:{provider}:platform/inbox_item/{sid}-{num}-{step}"
        ctx["kwrite"](nibx, {"schema": "platform.inbox_item", "id": nibx, "tenant": provider,
                             "actor": f"role://{provider}/{role}", "title": f"{off.get('title') or sid}: {step}",
                             "body": nxt.get("task") or "", "ref": ntid, "kind": "task", "status": "unread",
                             "created_at": now, "scope": case.get("scope")})
        ctx["kwrite"](case["id"], {**case, "current_step": step, "updated_at": now})
        return {"advanced_to": step, "routed_to": role,
                **({"issue": issue} if issue else {}), "txn": {"status": "in_progress", "step": step}}, None
    ctx["kwrite"](case["id"], {**case, "status": "closed", "current_step": "resolved", "closed_at": now})
    return {"case_closed": True, "advanced_to": "resolved",
            **({"issue": issue} if issue else {}), "txn": {"status": "resolved", "step": "resolved"}}, None


def h_service_open(ctx):
    """ADR-0097 companion to service-advance — the case-OPENING interpreter, ported out of app_host's `_provider_fulfil`
    (the reference BPP). Given an offering + the citizen's submission + pre-materialised scope, interpret the offering's
    `fulfillment.workflow` to shape the service CASE, the civic.submission, and the FIRST workflow task + its inbox item
    routed to the first role. Pure interpretation: NO network, NO writes.

    Why it returns a WRITE-SET instead of writing (unlike service-advance): the open path persists under the PROVIDER's
    authority (a cross-tenant write into the provider tenant, on behalf of a citizen who is NOT in it) — an identity a
    clean-room engine legitimately cannot assume. So the engine owns the workflow LOGIC (which records, first step,
    role, titles) and hands the host a list of {id, object} to persist with the provider token."""
    body = ctx["body"]
    off = body.get("offering") or {}
    message = dict(body.get("message") or {})
    citizen_sub = body.get("citizen_sub") or ""
    csc = body.get("scope")                                   # host pre-materialised against the provider org tree (transport)
    provider = off.get("tenant"); sid = off.get("service_id")
    sol = off.get("solution"); now = _now()
    num = _u.uuid4().hex[:6].upper()
    case_id = f"ds:{provider}:case/{sid}-{num}"               # PROVIDER-assigned case reference (its own numbering)
    writes = [{"id": case_id, "object": {
        "schema": "platform.case", "id": case_id, "tenant": provider, "status": "open",
        "title": (off.get("title") or sid) + " — application " + num, "opened_at": now,
        "opened_by": citizen_sub, "current_step": "received", "service": sid,
        "subjects": [off.get("id")] + (message.get("_verified_credentials") or []),   # + reused VCs as evidence
        "scope": csc, "workflow": sol}}]                      # the CERTIFIED solution fulfilling this case
    submission_id = None
    if message.get("form"):                                   # persist the citizen's STRUCTURED submission (→ VC claims later)
        submission_id = f"ds:{provider}:civic/submission/{sid}-{num}"
        writes.append({"id": submission_id, "object": {"schema": "civic.submission", "id": submission_id,
            "tenant": provider, "case": case_id, "service": sid, "holder": citizen_sub,
            "data": message.get("form"), "submitted_at": now}})
    # TRIGGER THE WORKFLOW: the first declared step becomes a task in the assigned ROLE's inbox (service-type → workflow
    # → role-inbox routing). Falls back to a single-step {assign_role, first_task} shorthand.
    routed_to = None; ff = off.get("fulfillment") or {}
    wf = ff.get("workflow") or []
    first = wf[0] if wf else ({"role": ff.get("assign_role"), "task": ff.get("first_task"), "step": "triage"}
                              if ff.get("assign_role") else None)
    if first and first.get("role"):
        role = first["role"]; step = first.get("step") or "triage"
        task_id = f"ds:{provider}:task/{sid}-{num}-{step}"
        writes.append({"id": task_id, "object": {"schema": "platform.task", "id": task_id, "tenant": provider,
            "case": case_id, "title": first.get("task") or ("Process " + (off.get("title") or sid)),
            "verb": "review", "assignee": f"role://{provider}/{role}", "status": "open",
            "created_at": now, "workflow_step": step, "scope": csc}})
        ibx_id = f"ds:{provider}:platform/inbox_item/{sid}-{num}-{step}"
        writes.append({"id": ibx_id, "object": {"schema": "platform.inbox_item", "id": ibx_id, "tenant": provider,
            "actor": f"role://{provider}/{role}", "title": f"New {off.get('title') or sid} · {num}",
            "body": (message.get("description") or "")[:280], "ref": task_id, "kind": "task",
            "status": "unread", "created_at": now, "scope": csc}})
        routed_to = role
    return {"writes": writes, "case": case_id, "reference": f"{sid.upper()}-{num}", "status": "received",
            "fulfilled_by": sol, "submission_id": submission_id,
            **({"routed_to": routed_to} if routed_to else {})}, None


# ================= ADR-0097 W1 — the unified WORKFLOW GRAPH interpreter =============================
# `platform.workflow`: typed nodes (task|decision|effect|gateway|timer) over the platform.case spine.
# Edges = `after` deps (+ a decision node's `transitions`). ONE engine — a strict SUPERSET of the FSM above:
# it reuses _run_effects / _authorized / _approver / _task_scope. Per-case node-state lives in the case's
# FREEFORM `scope._graph = {done:[], open:[], waiting:[], decisions:{node:transition}}` (no schema change).
# request → open a case + materialise ready nodes; update → complete a task node → unblock; decide → pick a
# decision transition → branch; effect/gateway auto-run; timer registers a platform.schedule wake-up (W1b tick).

def _gwf(ctx, workflow):
    return next((d for d in ctx["kfind"]("platform.workflow")
                 if d.get("workflow") == workflow and d.get("enabled", True)), None)

def _gnode(decl, nid):
    return next((n for n in (decl.get("nodes") or []) if n.get("id") == nid), None)

def _gdep_done(dep, done, decisions):
    if ":" in str(dep):                                        # "node:transition" → satisfied only if that branch was taken
        nid, tr = str(dep).split(":", 1); return decisions.get(nid) == tr
    return dep in done

def _gready(node, done, decisions):
    return all(_gdep_done(d, done, decisions) for d in (node.get("after") or []))

def _gstate(case):
    return dict((case.get("scope") or {}).get("_graph") or {"done": [], "open": [], "waiting": [], "decisions": {}})

def _g_actions(node, workflow, case_id):
    nid = node["id"]
    if node.get("kind") == "decision":
        return [_json.dumps({"label": (t.get("label") or t["name"].title()), "verb": "decide",
                             "style": t.get("style") or "pri",
                             "args": {"workflow": workflow, "case": case_id, "node": nid, "transition": t["name"]}})
                for t in (node.get("transitions") or [])]
    return [_json.dumps({"label": (node.get("label") or "Complete"), "verb": "update", "style": "pri",
                         "args": {"workflow": workflow, "case": case_id, "node": nid}})]

def _g_route(ctx, node, case, workflow, now):
    """Route a task/decision node to its actor's Inbox (a user via route.to_field on the case scope, or a role)."""
    tenant = case["tenant"]; route = node.get("route") or {}
    approver = _approver(route, case.get("scope") or {}, tenant) or (
        f"role://{tenant}/{node['assignee_role']}" if node.get("assignee_role") else None)
    if not approver:
        return None
    iid = f"ds:{tenant}:platform/inbox_item/{workflow.replace('.','-')}-{node['id']}-{case['id'].rsplit('/',1)[-1]}"
    ctx["kwrite"](iid, {"schema": "platform.inbox_item", "id": iid, "tenant": tenant, "actor": approver,
                        "title": route.get("title") or f"{workflow}: {node['id']}", "ref": case["id"],
                        "kind": "task", "status": "unread", "created_at": now,
                        "actions": _g_actions(node, workflow, case["id"]),
                        **_task_scope(ctx, route, {"record": case, "payload": {}, "case": case["id"],
                                                   "sub": case.get("opened_by"), "tenant": tenant, "now": now})})
    return approver

def _g_register_timer(ctx, node, case, now):
    """Best-effort platform.schedule wake-up for a timer/sla (W1b proves the round-trip end-to-end). Safe on shape drift."""
    try:
        tenant = case["tenant"]
        sid = f"ds:{tenant}:platform/schedule/wf-{case['id'].rsplit('/',1)[-1]}-{node['id']}"
        ctx["kwrite"](sid, {"schema": "platform.schedule", "id": sid, "tenant": tenant, "enabled": True,
                            "run_after": node.get("wait"), "run_at": node.get("until"), "verb": "tick",
                            "component": "ds:airawatdeveloper:engine/lifecycle-engine",
                            "body": {"case": case["id"], "node": node["id"]}, "created_at": now})
    except Exception as e:
        print("[graph] timer register skipped:", str(e)[:80], flush=True)

def _close_node_task(ctx, case_id, nid):
    for t in ctx["kfind"]("platform.inbox_item"):
        if t.get("ref") == case_id and t.get("kind") == "task" and t.get("status") != "done" and ("-"+nid+"-") in str(t.get("id","")):
            ctx["kwrite"](t["id"], {**t, "status": "done"})

def _advance_graph(ctx, decl, case_id):
    """Materialise EVERY ready-not-active node: run effect/gateway nodes immediately + recurse; route task/decision
    nodes to Inboxes; register timers. Loops until nothing new is ready. Idempotent (skips active/done nodes)."""
    case = ctx["kget"](case_id)
    if not case:
        return []
    tenant = case["tenant"]; workflow = decl["workflow"]; now = _now()
    st = _gstate(case); done = set(st.get("done") or []); openn = set(st.get("open") or [])
    waiting = set(st.get("waiting") or []); decisions = dict(st.get("decisions") or {}); routed = []
    changed = True
    while changed:
        changed = False
        for n in (decl.get("nodes") or []):
            nid = n["id"]; kind = n.get("kind")
            if nid in done or nid in openn or nid in waiting or nid == "END":
                continue
            if not _gready(n, done, decisions):
                continue
            if kind in ("task", "decision"):
                who = _g_route(ctx, n, case, workflow, now); openn.add(nid)
                if who: routed.append({"node": nid, "to": who})
                changed = True
            elif kind == "effect":
                _run_effects(ctx, n.get("effects"), {"record": case, "payload": {}, "case": case_id,
                             "sub": case.get("opened_by"), "tenant": tenant, "now": now},
                             subjects=case.setdefault("subjects", []))
                done.add(nid); changed = True
            elif kind == "gateway":
                done.add(nid); changed = True                 # readiness already enforced the join/split via `after`
            elif kind == "timer":
                _g_register_timer(ctx, n, case, now); waiting.add(nid); changed = True
    scope = dict(case.get("scope") or {})
    scope["_graph"] = {"done": sorted(done), "open": sorted(openn), "waiting": sorted(waiting), "decisions": decisions}
    live = bool(openn or waiting)
    ctx["kwrite"](case_id, {**case, "scope": scope,
                            "current_step": (sorted(openn)[0] if openn else ("waiting" if waiting else "closed")),
                            "status": "open" if live else "closed", "updated_at": now})
    return routed

def h_graph_request(ctx, decl):
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    workflow = decl["workflow"]; payload = dict(body.get("payload") or {}); now = _now()
    prep = decl.get("prepare") or {}                          # optional context provider (same as FSM)
    if prep.get("engine") and prep.get("verb") and ctx.get("call"):
        try:
            pres, perr = ctx["call"](prep["engine"], prep["verb"], dict(payload, **(prep.get("args") or {})))
        except Exception as e:
            return None, "prepare failed: " + str(e)[:120]
        if perr:
            return None, perr if isinstance(perr, str) else "prepare failed"
        if isinstance(pres, dict):
            for k, v in pres.items(): payload.setdefault(k, v)
    cid = f"ds:{tenant}:case/{workflow.replace('.','-')}-{_u.uuid4().hex[:8]}"
    cf = set()                                                # denormalise routing fields onto the case scope (spine convention)
    for n in (decl.get("nodes") or []):
        r = n.get("route") or {}
        if r.get("to_field"): cf.add(r["to_field"])
    scope = {k: payload.get(k) for k in cf if payload.get(k) is not None}
    scope["_graph"] = {"done": [], "open": [], "waiting": [], "decisions": {}}
    ctx["kwrite"](cid, {"schema": "platform.case", "id": cid, "tenant": tenant, "workflow": workflow, "status": "open",
                        "current_step": decl.get("start") or "start", "opened_by": sub, "opened_at": now,
                        "updated_at": now, "subjects": [], "scope": scope,
                        "title": str(payload.get("title") or payload.get("name") or workflow)})
    routed = _advance_graph(ctx, decl, cid)
    case = ctx["kget"](cid)
    return {"case": cid, "workflow": workflow, "status": case.get("status"), "step": case.get("current_step"), "routed": routed}, None

def h_graph_update(ctx, decl):
    """Complete a `task` node → mark done, unblock dependents."""
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    workflow, case_id, nid = decl["workflow"], body.get("case"), body.get("node")
    case = ctx["kget"](case_id)
    if not case: return None, "case not found"
    node = _gnode(decl, nid)
    if not node or node.get("kind") != "task": return None, f"'{nid}' is not a task node"
    st = _gstate(case)
    if nid not in (st.get("open") or []): return None, f"task '{nid}' is not open"
    now = _now(); scope = dict(case.get("scope") or {}); g = dict(scope.get("_graph") or {})
    g["open"] = [x for x in (g.get("open") or []) if x != nid]; g.setdefault("done", []).append(nid); scope["_graph"] = g
    for k, v in (body.get("patch") or {}).items():
        if k != "_graph": scope[k] = v                        # a task may collect data onto the case
    case_u = {**case, "scope": scope, "updated_at": now}
    if node.get("effects"):
        _run_effects(ctx, node.get("effects"), {"record": case_u, "payload": body.get("payload") or {}, "case": case_id,
                     "sub": sub, "tenant": tenant, "now": now}, subjects=case_u.setdefault("subjects", []))
    ctx["kwrite"](case_id, case_u); _close_node_task(ctx, case_id, nid); _advance_graph(ctx, decl, case_id)
    case2 = ctx["kget"](case_id)
    return {"case": case_id, "node": nid, "completed": True, "status": case2.get("status"), "step": case2.get("current_step")}, None

def h_graph_decide(ctx, decl):
    """Complete a `decision` node → pick a transition → branch."""
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    workflow, case_id, nid, tname = decl["workflow"], body.get("case"), body.get("node"), body.get("transition")
    case = ctx["kget"](case_id)
    if not case: return None, "case not found"
    node = _gnode(decl, nid)
    if not node or node.get("kind") != "decision": return None, f"'{nid}' is not a decision node"
    st = _gstate(case)
    if nid not in (st.get("open") or []): return None, f"decision '{nid}' is not open"
    tr = next((t for t in (node.get("transitions") or []) if t.get("name") == tname), None)
    if not tr: return None, f"unknown transition '{tname}'"
    ok, why = _authorized({**node, **tr}, case, ctx)          # node.authorize (OR-form) or tr overrides
    if not ok: return None, why
    now = _now(); vars = {"record": case, "payload": body.get("payload") or {}, "case": case_id, "sub": sub, "tenant": tenant, "now": now}
    case_u = dict(case)
    _run_effects(ctx, tr.get("effects"), {**vars, "record": case_u}, subjects=case_u.setdefault("subjects", []))
    scope = dict(case_u.get("scope") or {})
    scope.update(_resolve(tr.get("stamp") or {}, vars))       # stamp → freeform scope (platform.case root is schema-closed)
    g = dict(scope.get("_graph") or {})
    g["open"] = [x for x in (g.get("open") or []) if x != nid]; g.setdefault("done", []).append(nid)
    g.setdefault("decisions", {})[nid] = tname; scope["_graph"] = g; case_u["scope"] = scope
    ctx["kwrite"](case_id, case_u); _close_node_task(ctx, case_id, nid); _advance_graph(ctx, decl, case_id)
    case2 = ctx["kget"](case_id)
    return {"case": case_id, "node": nid, "transition": tname, "status": case2.get("status"), "step": case2.get("current_step")}, None

def h_graph_status(ctx, decl):
    case = ctx["kget"](ctx["body"].get("case"))
    if not case: return None, "case not found"
    tasks = [i for i in ctx["kfind"]("platform.inbox_item") if i.get("ref") == case["id"]]
    return {"case": case["id"], "status": case.get("status"), "step": case.get("current_step"),
            "graph": _gstate(case), "record": case,
            "tasks": [{"id": t["id"], "actor": t.get("actor"), "kind": t.get("kind"), "status": t.get("status")} for t in tasks]}, None

def h_tick(ctx):
    """W1b: scheduler wake-up for a timer/sla node — fire the timer (mark done + advance) or apply an SLA rung."""
    body = ctx["body"]; case = ctx["kget"](body.get("case")) if body.get("case") else None
    if not case: return {"tick": "no-case"}, None
    decl = _gwf(ctx, case.get("workflow"))
    if not decl: return {"tick": "no-graph-decl"}, None
    nid = body.get("node"); node = _gnode(decl, nid); st = _gstate(case)
    if node and node.get("kind") == "timer" and nid in (st.get("waiting") or []):
        scope = dict(case.get("scope") or {}); g = dict(scope.get("_graph") or {})
        g["waiting"] = [x for x in (g.get("waiting") or []) if x != nid]; g.setdefault("done", []).append(nid)
        scope["_graph"] = g; ctx["kwrite"](case["id"], {**case, "scope": scope})
        _advance_graph(ctx, decl, case["id"])
        return {"tick": "timer-fired", "node": nid}, None
    return {"tick": "noop"}, None


# ADR-0091/0097 canonical Lifecycle verbs are the BARE names (request/decide/update/withdraw/status); the
# namespaced `lifecycle-*` keys are retained so the pre-#263 bridge verbs keep resolving during migration.
HANDLERS = {
    "request": h_request, "decide": h_decide, "status": h_status, "update": h_update, "withdraw": h_withdraw,
    "lifecycle-request": h_request, "lifecycle-decide": h_decide, "lifecycle-status": h_status,
    "lifecycle-update": h_update, "lifecycle-withdraw": h_withdraw,
    "service-advance": h_service_advance,   # ADR-0097: the ported service-request workflow interpreter (advance)
    "service-open": h_service_open,         # ADR-0097: the ported service-request case opener (first step)
    "tick": h_tick,                         # ADR-0097 W1/W1b: scheduler wake-up for a timer / SLA node on a graph case
}


def main():
    print("Lifecycle Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
