"""Org Foundation — Project layer: govern the `project` domain + 5 schemas, and bump finance.budget → +project.

THE MODEL. A project is a RESULTS CONTRACT binding why→how→proof→cost, then tracked against reality:
    OUTCOME (why)  ◄contributes  OUTPUT (how)  ◄delivered  DELIVERABLE ─due─► MILESTONE   ◄funded  OUTLAY→BUDGET
The results chain is embedded in a VERSIONED project.proposal = the BASELINE (approve → active; adjust → new version
that supersedes, needs re-approval). Approval is a decided change-request (ADR-0056) → a governance.decision. Actuals
come from REAL finance.transaction / hr.expense_claim already tagged with `project` — the project is the roll-up axis.
"""
import json, urllib.request, urllib.error, http.cookiejar
APP="http://localhost:8099"; NET="http://localhost:8090"; IDN="http://localhost:8093"
ID_PAT=r"^ds:[a-z0-9-]+:\S+$"
def post(url, body, token=None, cj=None):
    req=urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}, method="POST")
    if token: req.add_header("Authorization","Bearer "+token)
    op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(op.open(req,timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:240]}"
STOK=post(IDN+"/realms/airawat/login",{"username":"steward"})[0]["access_token"]
cj=http.cookiejar.CookieJar(); post(APP+"/login",{"realm":"airawatdeveloper","username":"devrick"},cj=cj)
S=lambda:{"type":"string"}; Sn=lambda:{"type":["string","null"]}; N=lambda:{"type":"number"}; Nn=lambda:{"type":["number","null"]}
B=lambda:{"type":["boolean","null"]}; A=lambda:{"type":"array","items":{"type":"object"}}
def defn(sid,kind,reqd,props,desc):
    p={"schema":{"const":sid},"id":{"type":"string","pattern":ID_PAT}}; p.update(props)
    return {"type":"object","x-kind":kind,"additionalProperties":False,"required":["schema","id"]+reqd,"properties":p,"title":sid,"description":desc}

# --- 1) govern the `project` domain --------------------------------------------------------------------------
r,err=post(NET+"/v1/domains",{"domain":"project","label":"Project"},token=STOK)
print("domain project:", ("OK" if r else ("(exists?) "+str(err))))

# --- 2) the schemas ------------------------------------------------------------------------------------------
SCHEMAS={
 # THE ENTITY — thin spine. The rich content lives in the current baseline (project.proposal). status is the lifecycle.
 "project.project": ("entity",["code","name","org_unit","status"],{
    "code":S(),"name":S(),"org_unit":S(),                 # owner unit — the roll-up axis for unit × project
    "objective":Sn(),                                     # one-line WHY (the headline outcome)
    "sponsor":Sn(),                                       # accountable person/role (the approver of record)
    "start_date":Sn(),"end_date":Sn(),
    "status":{"enum":["draft","submitted","approved","active","review","evaluation","closed","rejected"]},
    "baseline_version":Nn(),                              # the approved project.proposal version currently in force
    "currency":Sn(),"tenant":Sn(),"created_at":Sn()},
    "A PROJECT: an org-unit-owned results contract. Thin entity; the results chain + outlay live in its current baseline (project.proposal). status drives the lifecycle."),
 # THE BASELINE — a versioned proposal. outcomes/outputs/milestones/deliverables/outlay are embedded arrays (one
 # coherent, approvable, re-baselineable document). Approve → in force; adjust → new version supersedes (re-approval).
 "project.proposal": ("record",["project","version","status"],{
    "project":S(),"version":N(),
    "status":{"enum":["draft","submitted","approved","superseded","rejected"]},
    "title":Sn(),"summary":Sn(),
    "outcomes":A(),      # [{id, statement, indicator, baseline, target}]                    — WHY
    "outputs":A(),       # [{id, statement, outcome, indicator, target}]                     — HOW (each → an outcome)
    "milestones":A(),    # [{id, name, due_date, outputs:[id], status}]                      — dated checkpoints
    "deliverables":A(),  # [{id, name, milestone, output, status}]                           — concrete artifacts
    "outlay":A(),        # [{id, output, resource_type, description, quantity, unit, rate, amount}]  — resources × rates
    "total_outlay":Nn(),"currency":Sn(),
    "submitted_by":Sn(),"submitted_at":Sn(),
    "decision":Sn(),     # the governance.decision that approved/rejected this version (decided change-request)
    "approved_at":Sn(),"reason":Sn(),
    "supersedes":Nn(),"tenant":Sn(),"created_at":Sn()},
    "A PROJECT BASELINE (versioned proposal): the embedded results chain outcomes→outputs→milestones/deliverables and the outlay (resources×rates) that sums to the budget. Approval = a governance.decision."),
 # PERIODIC REVIEW during execution — progress vs the in-force baseline. Actuals sourced from posted finance.transactions.
 "project.review": ("record",["project","at"],{
    "project":S(),"at":S(),"period":Sn(),
    "milestone_progress":Nn(),                            # % of milestones completed
    "planned_to_date":Nn(),"spent_to_date":Nn(),"variance":Nn(),   # ₹ plan vs actual (posted txns tagged project)
    "status":{"enum":["on_track","at_risk","off_track","unknown"]},
    "narrative":Sn(),"flags":A(),"reviewed_by":Sn(),"tenant":Sn(),"created_at":Sn()},
    "A PROJECT REVIEW: a dated progress check during execution — milestone %, spend-to-date vs plan, variance, RAG status. Actuals are real posted transactions."),
 # EVALUATION REPORT — end-of-project (or phase) assessment: were the OUTCOMES achieved, OUTPUTS delivered, money worth it.
 "project.evaluation": ("record",["project","at","recommendation"],{
    "project":S(),"at":S(),
    "outcomes_assessment":A(),   # [{outcome, target, achieved, rating}]
    "outputs_delivered":A(),     # [{output, planned, delivered}]
    "outlay_planned":Nn(),"outlay_actual":Nn(),
    "value_for_money":Sn(),      # narrative VfM judgement
    "rating":{"enum":["highly_satisfactory","satisfactory","moderate","unsatisfactory","unknown"]},
    "lessons":A(),
    "recommendation":{"enum":["continue","close","scale","redesign"]},
    "evaluator":Sn(),"decision":Sn(),"tenant":Sn(),"created_at":Sn()},
    "A PROJECT EVALUATION REPORT: outcomes achieved vs target, outputs delivered vs planned, outlay actual vs planned, value-for-money, lessons, and a forward recommendation."),
 # ALLOCATION — a person's assignment to a project (%). The basis for apportioning labour cost + capacity planning.
 "project.allocation": ("record",["employee","project","percent"],{
    "employee":S(),"project":S(),"percent":N(),           # 0–100 of the person's time
    "role_on_project":Sn(),"from_date":Sn(),"to_date":Sn(),
    "status":{"enum":["active","ended"]},"tenant":Sn(),"created_at":Sn()},
    "A PROJECT ALLOCATION: an employee assigned to a project at a % of time — apportions labour to the project and feeds capacity planning."),
}
for sid,(kind,reqd,props,desc) in SCHEMAS.items():
    d=defn(sid,kind,reqd,props,desc)
    pr,pe=post(APP+"/bridge/schema-request",{"schema_id":sid,"version":"0.1.0","definition":d,"x_kind":kind,"justification":"Project layer: outlays→outputs→outcomes, plan vs actual, decided-change-request lifecycle."},cj=cj)
    if pe: print(f"  {sid}: PROPOSE {pe}"); continue
    dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":sid,"version":"0.1.0","decision":"approve"},token=STOK)
    print(f"  {sid}: {'OK '+dr['published_schema'] if dr else 'ERR '+str(de)}")

# --- 3) bump finance.budget → 0.2.0 (+ project): a budget can be scoped to a project ------------------------
budget=defn("finance.budget","entity",["code","name","fiscal_year","allocated"],{
    "code":S(),"name":S(),"fiscal_year":S(),"account":Sn(),
    "org_unit":Sn(),"project":Sn(),                       # NEW: the project this budget head funds (roll-up axis)
    "allocated":N(),"committed":Nn(),"spent":Nn(),
    "currency":Sn(),"status":{"enum":["active","closed"]},"tenant":Sn(),"created_at":Sn()},
    "A budget head — an allocation on an expense account for a fiscal year/unit/PROJECT. available = allocated − committed − spent.")
pr,pe=post(APP+"/bridge/schema-request",{"schema_id":"finance.budget","version":"0.2.0","definition":budget,"x_kind":"entity","justification":"Budget can be scoped to a project (plan vs actual roll-up)."},cj=cj)
if pe: print("  finance.budget@0.2.0: PROPOSE",pe)
else:
    dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":"finance.budget","version":"0.2.0","decision":"approve"},token=STOK)
    print("  finance.budget@0.2.0:", ('OK '+dr['published_schema']) if dr else ('ERR '+str(de)))
