"""ADR-0097 W0 — govern the unified `platform.workflow` GRAPH schema.

One declaration model that subsumes all three legacy workflow shapes:
  - `platform.lifecycle`         (FSM: states + apply/approve/reject transitions)   — hr.leave, approvals
  - `service.offering.workflow`  (ordered step-list: submit→verify→approve→issue)   — civic services
  - `platform.workflow` (ADR-0048 step-DAG: steps + after-deps + parallel)          — orchestration

A workflow is a GRAPH of typed nodes over the platform.case / Task spine (ADR-0048). Edges are `after`
(dependency edges → sequencing / parallelism / joins) + a `decision` node's `transitions` (branch edges →
outcomes, send-back cycles). Node kinds: task | decision | effect | gateway | timer. SLA / deadlines /
escalation are first-class (`sla`/`escalate` on task/decision; `timer` node), enforced via platform.schedule.
This is a SUPERSET of the current lifecycle-engine grammar (transitions carry from/to/route/authorize/stamp/
label/style) so W1 EXTENDS the engine rather than re-grammaring it. Deprecating the two legacy *shapes* happens
in W4 (once hr.leave etc. are migrated) — NOT here (they're still live).

Recipe (mirrors govern_fund_source.py): propose as the developer session → approve as the airawat steward →
refresh the kernel schema-cache. Run INSIDE app-host on EC2 (internal hostnames).
Env: APP NET IDN KERNEL.
"""
import json, os, re, urllib.request, urllib.error, http.cookiejar
APP=os.environ.get("APP","http://localhost:8099"); NET=os.environ.get("NET","http://network:8090")
IDN=os.environ.get("IDN","http://identity:8093"); KERNEL=os.environ.get("KERNEL","http://kernel:8080")
DEV_REALM="airawatdeveloper"; DEV_USER="devrick"; ID_PAT=r"^ds:[a-z0-9-]+:\S+$"

def post(url, body, token=None, cj=None):
    req=urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}, method="POST")
    if token: req.add_header("Authorization","Bearer "+token)
    op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(op.open(req,timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:280]}"

# ---- the unified graph schema (JSON Schema; x-kind record, additionalProperties:False) --------------
# A NODE: required {id, kind}; the union of per-kind fields is optional (conformance in W6 enforces the
# per-kind rules — e.g. a `decision` must carry `transitions`). Kept flat + permissive on nested shapes
# (route/guard/sla/effects are validated as objects, their internals by the engine + W6 suite).
NODE = {
  "type":"object","required":["id","kind"],"additionalProperties":False,
  "properties":{
    "id":{"type":"string"},
    "kind":{"enum":["task","decision","effect","gateway","timer"]},
    "after":{"type":"array","items":{"type":"string"}},          # dep edges: "<nodeId>" or "<nodeId>:<transition>"
    # task / decision routing + gating
    "route":{"type":["object","null"]},                          # {role | to_field, title}
    "assignee_role":{"type":["string","null"]},
    "guard":{"type":["object","null"]},                          # {ruleset} — ADR-0088 evaluate
    "notify":{"type":["object","array","null"]},
    "sla":{"type":["object","null"]},                            # {due_in|due_at, basis(wall|business), calendar?, escalate:[…]}
    "fulfilment":{},                                             # "local" | {provider|offering}
    # decision
    "authorize":{"type":["object","null"]},                      # {any:[{role}|{actor_field}]}
    "transitions":{"type":"array","items":{"type":"object"}},    # [{name,to,guard?,stamp?,label?,style?}]
    # effect
    "effects":{"type":"array","items":{"type":"object"}},        # [{write:{schema,…}} | {call:{verb,body}}]
    # gateway
    "mode":{"enum":["split","join"]},
    "to":{"type":["string","array","null"]},                     # gateway/timer target(s)
    # timer
    "wait":{"type":["string","null"]},                           # ISO-8601 duration
    "until":{"type":["string","null"]},                          # instant or "$record.<field>"
    # display
    "label":{"type":["string","null"]},"style":{"type":["string","null"]}
  }
}
DEFN = {
  "type":"object","x-kind":"record","additionalProperties":False,
  "required":["schema","id","workflow","start","nodes"],
  "properties":{
    "schema":{"const":"platform.workflow"},
    "id":{"type":"string","pattern":ID_PAT},
    "tenant":{"type":["string","null"]},
    "workflow":{"type":"string"},                                # domain key, e.g. "building.permit"
    "title":{"type":["string","null"]},
    "enabled":{"type":["boolean","null"]},
    "subject_schema":{"type":["string","null"]},                 # domain record the case references (it isn't the case)
    "start":{"type":"string"},                                   # entry node id
    "prepare":{"type":["object","null"]},                        # {engine,verb} optional pre-resolve (lifecycle compat)
    "nodes":{"type":"array","items":NODE},
    "version":{"type":["string","null"]},
    "created_at":{"type":["string","null"]},"observed_at":{"type":["string","null"]}
  },
  "title":"platform.workflow",
  "description":("ADR-0097 unified workflow GRAPH. Typed nodes (task|decision|effect|gateway|timer) over the "
    "platform.case/Task spine (ADR-0048); edges = `after` deps (+ decision `transitions` for branches/cycles). "
    "Per-node guard (ADR-0088 evaluate), route/assignee_role, sla+escalate + timer (deadlines/escalation via "
    "platform.schedule), effects (write|call), fulfilment (local|{provider|offering}). Subsumes platform.lifecycle "
    "(FSM), service.offering.workflow (step-list) and the ADR-0048 step-DAG. Run by the lifecycle-engine via the "
    "generic verbs request/decide/update/withdraw/status.")
}

def existing_versions(cj):
    """Find already-governed platform.workflow versions so we propose the next MAJOR (unified graph)."""
    r,_=post(APP+"/bridge/schemas",{},cj=cj); vers=[]
    for s in ((r or {}).get("schemas") or []):
        sid=s.get("schema_id") or s.get("id") or s.get("schema") or s.get("name") or ""
        if sid=="platform.workflow" or str(sid).startswith("platform.workflow@"):
            v=s.get("version") or (sid.split("@")[1] if "@" in str(sid) else None)
            if v: vers.append(str(v))
    return vers

def main():
    stok=post(IDN+"/realms/airawat/login",{"username":"steward"})[0]["access_token"]
    cj=http.cookiejar.CookieJar(); post(APP+"/login",{"realm":DEV_REALM,"username":DEV_USER},cj=cj)
    have=existing_versions(cj)
    majors=[int(v.split(".")[0]) for v in have if re.match(r"^\d+\.", v)]
    version = f"{(max(majors)+1) if majors else 1}.0.0"        # next major → the unified graph; 1.0.0 if new
    print(f"platform.workflow existing versions: {have or '(none)'} → proposing @{version}")
    pr,pe=post(APP+"/bridge/schema-request",{"schema_id":"platform.workflow","version":version,"definition":DEFN,
        "x_kind":"record","justification":"ADR-0097 W0: unified workflow GRAPH (task|decision|effect|gateway|timer + "
        "sla/escalate/timer) — one model subsuming platform.lifecycle (FSM), service.offering.workflow (step-list), "
        "ADR-0048 step-DAG. Superset of the lifecycle-engine grammar; W1 extends the engine to interpret it."},cj=cj)
    if pe: print("  PROPOSE:", pe); return
    dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":"platform.workflow","version":version,"decision":"approve"},token=stok)
    print("  APPROVE:", (("OK "+str(dr.get("published_schema",""))) if dr else ("ERR "+str(de))))
    kc,ke=post(KERNEL+"/v1/registry/schema-cache/refresh",{},token=stok); print("  schema-cache refresh:", kc or ke)

if __name__=="__main__":
    main()
