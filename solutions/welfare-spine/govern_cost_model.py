#!/usr/bin/env python3
"""Govern `welfare.cost_model` (ADR-0069 slice 2) — per-capability INDICATIVE cost to raise ONE person above the
floor: a fiscal ₹cr/person + the environmental resource draw/person. Governed as DATA (transparent + editable,
like the floors), so the prioritisation-engine reads it and the budget bites automatically — no hand-passed
costs. Numbers are indicative (replace with a real DPR/norm); every one carries its basis + confidence.
Advise-only by default; --publish to govern. Env AIROS_APP/AIROS_NET/AIROS_IDN (default local)."""
import os, sys, json, urllib.request, urllib.error, http.cookiejar
APP=os.environ.get("AIROS_APP","http://localhost:8099"); NET=os.environ.get("AIROS_NET","http://localhost:8090"); IDN=os.environ.get("AIROS_IDN","http://localhost:8093")
ID_PAT=r"^ds:[a-z0-9-]+:\S+$"
def _post(url, body, token=None, cj=None):
    req=urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}, method="POST")
    if token: req.add_header("Authorization","Bearer "+token)
    op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(op.open(req,timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:220]}"
Nn=lambda:{"type":["number","null"]}; Sn=lambda:{"type":["string","null"]}; An=lambda:{"type":["array","null"]}
MODEL={
  "type":"object","x-kind":"entity","additionalProperties":False,
  "required":["schema","id","capability","fiscal_cr_per_person"],
  "title":"welfare.cost_model",
  "description":("Indicative cost to raise ONE person above the floor for a capability: fiscal_cr_per_person "
    "(₹crore, capex + first-year, INDICATIVE — replace with a cited DPR/norm) + env_draws (the environmental "
    "resource draw per person, checked against env.resource_budget). Governed as data so the prioritisation-engine "
    "reads it and the fiscal + environmental budgets bite without hand-config."),
  "properties":{
    "schema":{"const":"welfare.cost_model"},"id":{"type":"string","pattern":ID_PAT},
    "capability":{"type":"string"},                                 # welfare.capability id
    "fiscal_cr_per_person":{"type":"number"},                       # ₹crore per person raised above the floor
    "env_draws":An(),                                              # [{resource, amount_per_person, unit}]
    "basis":Sn(),                                                  # what the number is based on
    "source":Sn(),                                                 # ref.source id
    "method":{"enum":["estimated","dpr","norm","declared"]},
    "confidence":Nn(),"tenant":Sn(),"created_at":Sn()}}
def advise():
    r,e=_post(NET+"/v1/schemas/advise",{"schema_id":"welfare.cost_model","definition":MODEL,"kind":"schema"})
    if e: print("advise ERR",e); return
    print("house_rule_violations:", r.get("house_rule_violations") or "none", "| rec:", r.get("recommendation"))
def govern():
    st,_=_post(IDN+"/realms/airawat/login",{"username":"steward"}); stoken=st["access_token"]
    cj=http.cookiejar.CookieJar(); _post(APP+"/login",{"realm":"airawatdeveloper","username":"devrick"},cj=cj)
    pr,pe=_post(APP+"/bridge/schema-request",{"schema_id":"welfare.cost_model","version":"0.1.0","definition":MODEL,"x_kind":"entity","justification":"ADR-0069 — per-capability cost model so the budget bites from governed data."},cj=cj)
    if pe: print("propose ERR",pe); return
    dr,de=_post(NET+"/v1/schema-requests/decide",{"schema_id":"welfare.cost_model","version":"0.1.0","decision":"approve"},token=stoken)
    print("welfare.cost_model:", f"propose={pr.get('status')} approve={'OK '+dr['published_schema'] if dr else 'ERR '+str(de)}")
if __name__=="__main__":
    (govern if "--publish" in sys.argv else advise)()
