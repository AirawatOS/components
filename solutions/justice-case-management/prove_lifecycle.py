#!/usr/bin/env python3
"""Justice Case Management — LOCAL end-to-end lifecycle proof (ADR-0076 process story enacted).

File (advocate) -> Scrutiny (+return loop capable) -> Register (registrar) -> Schedule hearing (bench clerk)
-> Hold hearing + appearance (judge) -> Dispose (judge). Writes REAL governed justice.* facts AND advances
the generic platform.case spine + routes platform.task to the owning role. Reads state back at the end.
"""
import json, urllib.request, urllib.error, os, datetime

IDN = os.environ.get("IDN", "http://localhost:8093"); K = os.environ.get("K", "http://localhost:8080")
TEN = "court-1"

def _req(method, url, body=None, token=None, internal=None):
    r = urllib.request.Request(url, data=(json.dumps(body).encode() if body is not None else None),
                               method=method, headers={"Content-Type": "application/json"})
    if token: r.add_header("Authorization", "Bearer " + token)
    if internal: r.add_header("X-Internal-Token", internal)
    try: return json.loads(urllib.request.urlopen(r, timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:220]}"

def provision(u, roles): _req("POST", f"{IDN}/users", {"username": u, "tenant": TEN, "roles": roles}, internal="airawat-identity-internal")
def token(u):
    r, err = _req("POST", f"{IDN}/realms/{TEN}/login", {"username": u})
    if err: raise SystemExit(f"login {u}: {err}"); 
    return r["access_token"]
def now(d=0): return (datetime.datetime(2026,9,1,10,0,0)+datetime.timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
def write(tok, oid, obj, who):
    obj = dict(obj); obj["id"] = oid; obj.setdefault("tenant", TEN)
    r, err = _req("POST", f"{K}/v1/registry/objects", {"id": oid, "object": obj}, token=tok)
    print(f"  {'✓' if r else '✗'} [{who:15}] {obj.get('schema'):24} {oid.split('/',1)[-1]:24} {'v'+r['stored']['version'] if r else err}")
    return r
def resolve(tok, schema):
    r, err = _req("POST", f"{K}/v1/registry/resolve", {"filter": {"schema": schema}, "limit": 50}, token=tok)
    if err: return []
    rows = r.get("results", r.get("objects", [])) if isinstance(r, dict) else r
    return [x.get("object", x) for x in rows]

for u, roles in {"ca1":["justice.advocate"],"so1":["justice.scrutiny-officer"],"rg1":["justice.registrar"],
                 "bc1":["justice.bench-clerk"],"judge1":["justice.judge"]}.items():
    provision(u, roles)
T = token("court1-admin")
CASE="ds:court-1:justice/case/ct-2026-000101"; COURT="ds:court-1:justice/court/mc-01"; PC="ds:court-1:platform/case/pc-101"

def task(oid, verb, role, step, title):
    write(T, oid, {"schema":"platform.task","case":PC,"verb":verb,"status":"open","title":title,
                   "assignee":f"role://{role}","workflow_step":step,"created_at":now()}, "system→"+role.split('.')[-1])

print("① FILE  (Complainant Advocate — e-filing)")
write(T,"ds:court-1:justice/filing/f-101",{"schema":"justice.filing","case_ref":CASE,"mode":"e-filing",
      "status":"submitted","filing_type":"complaint","filed_at":now(0),"filed_by":"user://court-1/ca1",
      "filing_number":"FIL-2026-101","relief_sought":"Complaint under s.138 NI Act","observed_at":now(0)},"advocate")
write(T,CASE,{"schema":"justice.case","court_ref":COURT,"case_number":"","case_type":"criminal-complaint",
      "stage":"filing","status":"filed","filed_on":now(0),
      "identifiers":[{"scheme":"filing_number","value":"FIL-2026-101"}]},"advocate")
write(T,PC,{"schema":"platform.case","service":"justice-case-management","workflow":"justice.case_lifecycle",
      "title":"State v. — complaint (FIL-2026-101)","status":"active","current_step":"scrutiny","subjects":[CASE],
      "opened_at":now(0),"opened_by":"user://court-1/ca1","history":[{"event":"filed","at":now(0),"actor":"user://court-1/ca1"}]},"advocate")
task("ds:court-1:platform/task/t-scrutiny-101","verify","justice.scrutiny-officer","scrutiny","Scrutinise filing FIL-2026-101")

print("\n② SCRUTINISE  (Scrutiny Officer — counter) [accepted]")
write(T,"ds:court-1:justice/scrutiny_result/sr-101",{"schema":"justice.scrutiny_result","case_ref":CASE,
      "filing_ref":"ds:court-1:justice/filing/f-101","outcome":"accepted","defects":[],"scrutinised_at":now(1)},"scrutiny-officer")
task("ds:court-1:platform/task/t-register-101","approve","justice.registrar","registration","Register case (assign number)")

print("\n③ REGISTER  (Registrar — case number born)")
write(T,CASE,{"schema":"justice.case","court_ref":COURT,"case_number":"CC/101/2026","case_type":"criminal-complaint",
      "stage":"registered","status":"registered","filed_on":now(0),
      "identifiers":[{"scheme":"filing_number","value":"FIL-2026-101"},{"scheme":"case_number","value":"CC/101/2026"}]},"registrar")
task("ds:court-1:platform/task/t-schedule-101","fulfil","justice.bench-clerk","hearing","Schedule first hearing")

print("\n④ SCHEDULE HEARING  (Bench Clerk — on cause list)")
H1="ds:court-1:justice/hearing/h-101-1"
write(T,H1,{"schema":"justice.hearing","case_ref":CASE,"court_ref":COURT,"scheduled_for":now(14),
      "purpose":"appearance","bench":"user://court-1/judge1","mode":"physical","status":"on-cause-list","observed_at":now(3)},"bench-clerk")

print("\n⑤ HOLD HEARING + APPEARANCE  (Judge presides)")
write(T,H1,{"schema":"justice.hearing","case_ref":CASE,"court_ref":COURT,"scheduled_for":now(14),
      "purpose":"appearance","bench":"user://court-1/judge1","mode":"physical","status":"held",
      "outcome":"Parties present; heard and reserved for orders.","observed_at":now(14)},"judge")
write(T,"ds:court-1:justice/appearance/ap-101-1",{"schema":"justice.appearance","case_ref":CASE,"hearing_ref":H1,
      "appeared_by":"user://court-1/ca1","on_behalf_of":"complainant","appearance_type":"present",
      "observed_at":now(14),"recorded_at":now(14)},"bench-clerk")

print("\n⑥ DISPOSE  (Judge — judgment; case closed)")
write(T,"ds:court-1:justice/disposal/dp-101",{"schema":"justice.disposal","case_ref":CASE,"disposal_type":"judgment",
      "nature":"contested","in_favour_of":"complainant","disposed_at":now(30),"observed_at":now(30)},"judge")
write(T,CASE,{"schema":"justice.case","court_ref":COURT,"case_number":"CC/101/2026","case_type":"criminal-complaint",
      "stage":"disposed","status":"disposed","filed_on":now(0),"disposal_ref":"ds:court-1:justice/disposal/dp-101",
      "identifiers":[{"scheme":"case_number","value":"CC/101/2026"}]},"judge")
write(T,PC,{"schema":"platform.case","service":"justice-case-management","workflow":"justice.case_lifecycle",
      "title":"State v. — complaint (CC/101/2026)","status":"closed","current_step":"disposed","subjects":[CASE],
      "opened_at":now(0),"closed_at":now(30),"outcome":"ds:court-1:justice/disposal/dp-101",
      "history":[{"event":"filed","at":now(0)},{"event":"scrutiny-accepted","at":now(1)},{"event":"registered","at":now(1)},
                 {"event":"hearing-held","at":now(14)},{"event":"disposed","at":now(30)}]},"judge")

print("\n=== READBACK: the case as the court now holds it ===")
for c in resolve(T,"justice.case"):
    print(f"  case {c.get('case_number') or '(unregistered)'}  status={c.get('status'):11} stage={c.get('stage')}  disposal={c.get('disposal_ref','-')}")
for sch in ["justice.filing","justice.scrutiny_result","justice.hearing","justice.appearance","justice.disposal"]:
    print(f"  {sch:26} rows={len(resolve(T,sch))}")
pc=resolve(T,"platform.case")
if pc: print(f"  platform.case status={pc[0].get('status')} step={pc[0].get('current_step')} history={[h.get('event') for h in pc[0].get('history',[])]}")
tk=resolve(T,"platform.task")
print(f"  platform.task rows={len(tk)} routed→ {sorted({t.get('assignee','') for t in tk})}")
