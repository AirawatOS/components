"""Govern the GENERIC Lifecycle-plane VERBS as network.interface contracts (ADR-0091 V3 / ADR-0097, task #263).

ADR-0091 says verbs are few and load-bearing: instead of ~80 domain-scoped verbs (hr.leave_apply,
project.proposal_submit, justice.file_case, …), the Lifecycle plane has FIVE canonical verbs that operate on a
governed `platform.lifecycle` declaration — the domain lives in the declaration, not the verb name. This publishes
those five as signed `network.interface` contracts under a new `lifecycle` domain (the plane), so they appear in
the Verbs catalog, certify's governed-interfaces check enforces them, and domain verbs can be aliased onto them
(platform.verb_alias) with legacy fallback.

Run-dispatch name (kebab, app_host ENGINE_VERBS/BRIDGE_VERBS)  →  governed verb id (dot)
    lifecycle-request   → lifecycle.request     (open a case at the start node)
    lifecycle-decide    → lifecycle.decide      (complete a decision node / pick a transition)
    lifecycle-update    → lifecycle.update      (patch a task node's record, no transition)
    lifecycle-withdraw  → lifecycle.withdraw    (terminate the case)
    lifecycle-status    → lifecycle.status      (case state + open tasks)

Publishes directly as the airawat standards body (POST /v1/domains + /v1/interfaces, steward-gated). Idempotent.
"""
import json, os, urllib.request as u, urllib.error

NET = os.environ.get("NET_URL", "http://network:8090")
IDN = os.environ.get("IDN_URL", "http://identity:8093")
VER = "0.1.0"
N_LC = "platform.lifecycle"          # the governed declaration a lifecycle verb operates on


def post(url, body, hdr=None):
    r = u.Request(url, data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", **(hdr or {})}, method="POST")
    try:
        return json.loads(u.urlopen(r, timeout=60).read()), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code}:{e.read().decode()[:240]}"


def obj(required, props):
    d = {"type": "object", "properties": props}
    if required:
        d["required"] = required
    return d


# ---- the 5 generic Lifecycle verbs ----------------------------------------------------------------
_WF = {"type": "string", "description": "the platform.lifecycle workflow id (the declaration that carries the domain)", "x-ref": N_LC}
_CASE = {"type": "string", "description": "the case / domain-record id this verb acts on"}
_PAYLOAD = {"type": "object", "description": "domain fields for the operation (the intake form / patch)"}

INTERFACES = {
 "lifecycle.request": {
   "modality": "request", "x-actor": "requester",
   "description": "Open a new case on a governed lifecycle: run the start transition (guarded, routed), write the "
                  "domain record at its first state, and route the first task to the approver. The domain is "
                  "carried entirely by the referenced platform.lifecycle declaration — one engine, any workflow.",
   "request": obj(["workflow"], {"workflow": _WF, "payload": _PAYLOAD}),
   "response": obj(["case", "status"], {
       "case": {"type": "string", "description": "the opened case / record id"},
       "workflow": {"type": "string"}, "status": {"type": "string", "description": "the new state"},
       "routed_to": {"type": "string", "description": "actor the first task was routed to"}})},

 "lifecycle.decide": {
   "modality": "request", "x-actor": "approver",
   "description": "Complete a decision node on an open case: apply a named transition (legal-from-state + "
                  "authorized + guarded), record the decided change (state + stamp), fire effects, close the task, "
                  "route/notify the next actor. Authorization keys on (workflow, transition), per the declaration.",
   "request": obj(["workflow", "case", "transition"], {
       "workflow": _WF, "case": _CASE,
       "transition": {"type": "string", "description": "the transition name to apply (e.g. approve|reject)"},
       "payload": _PAYLOAD}),
   "response": obj(["case", "status"], {
       "case": {"type": "string"}, "workflow": {"type": "string"}, "transition": {"type": "string"},
       "status": {"type": "string"}, "routed_to": {"type": "string"}})},

 "lifecycle.update": {
   "modality": "request", "x-actor": "requester",
   "description": "Patch an in-flight case's record WITHOUT a state transition (complete/edit a task node's data). "
                  "Only the opener or a current task-holder may update; the declaration's `updatable` list bounds "
                  "which fields may change; structural fields are never patchable.",
   "request": obj(["workflow", "case", "patch"], {
       "workflow": _WF, "case": _CASE, "patch": {"type": "object", "description": "field→value edits"}}),
   "response": obj(["case", "status"], {
       "case": {"type": "string"}, "workflow": {"type": "string"}, "status": {"type": "string"},
       "updated": {"type": "array", "items": {"type": "string"}}})},

 "lifecycle.withdraw": {
   "modality": "request", "x-actor": "requester",
   "description": "Terminate a case: set a terminal 'withdrawn' state, close open tasks, stamp who/when. Only the "
                  "opener, a role authorized on a declared withdraw transition, or a tenant admin may withdraw.",
   "request": obj(["workflow", "case"], {"workflow": _WF, "case": _CASE, "payload": _PAYLOAD}),
   "response": obj(["case", "status"], {
       "case": {"type": "string"}, "workflow": {"type": "string"}, "status": {"type": "string"}})},

 "lifecycle.status": {
   "modality": "request", "x-actor": "observer",
   "description": "Read a case's current state + its open tasks (a Data-plane read projection over the lifecycle).",
   "request": obj(["case"], {"case": _CASE}),
   "response": obj(["case", "status"], {
       "case": {"type": "string"}, "status": {"type": "string"}, "case_status": {"type": "string"},
       "record": {"type": "object"}, "tasks": {"type": "array", "items": {"type": "object"}}})},
}


def main():
    r, e = post(f"{IDN}/realms/airawat/login", {"username": "steward"})
    if not r:
        print("LOGIN FAILED:", e); return
    STOK = "Bearer " + r["access_token"]
    # 1) govern the `lifecycle` domain (the ADR-0091 Lifecycle plane) — idempotent
    dr, de = post(f"{NET}/v1/domains", {"domain": "lifecycle", "label": "Lifecycle plane (ADR-0091): generic case verbs"}, {"Authorization": STOK})
    print("domain lifecycle:", ("ok" if dr else de))
    # 2) publish the 5 generic verbs
    ok = 0
    for iid, defn in INTERFACES.items():
        res, err = post(f"{NET}/v1/interfaces", {"interface_id": iid, "version": VER, "definition": defn}, {"Authorization": STOK})
        if res:
            ok += 1; print(f"  ✓ {iid}@{VER}  ({res['published']['modality']})")
        else:
            print(f"  ✗ {iid}: {err}")
    print(f"published {ok}/{len(INTERFACES)} lifecycle interfaces")
    cat = json.loads(u.urlopen(f"{NET}/v1/interfaces", timeout=20).read())["interfaces"]
    lc = sorted(i["interface_id"] for i in cat if i["interface_id"].startswith("lifecycle."))
    print("lifecycle.* now in catalog:", lc)


if __name__ == "__main__":
    main()
