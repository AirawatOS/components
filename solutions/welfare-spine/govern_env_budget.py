#!/usr/bin/env python3
"""Govern `env.resource_budget` (ADR-0069 slice 2, the sustainability constraint) — a carrying-capacity STOCK you
can overdraw: aquifer / airshed / land / carbon per jurisdiction, with capacity, regeneration rate, and current
draw. The prioritisation-engine checks an intervention's resource draw against the sustainable headroom
(regen - draw), so 'fix the gap' can never quietly exceed what the environment can bear. `env` domain already
governed. Advise-only by default; --publish to govern. Env AIROS_APP/AIROS_NET/AIROS_IDN (default local)."""
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
BUDGET={
  "type":"object","x-kind":"measure","additionalProperties":False,
  "required":["schema","id","subject","observed_at","prov","resource","capacity","unit"],
  "title":"env.resource_budget",
  "description":("A carrying-capacity STOCK you can overdraw — aquifer / airshed / land / carbon for a "
    "jurisdiction, with capacity, regeneration rate and current draw. Sustainable headroom = regen_rate - "
    "current_draw. The prioritisation-engine defers any intervention whose resource draw would exceed the "
    "headroom, so raising a floor can never silently breach what the environment can bear. subject = the "
    "jurisdiction the budget applies to."),
  "properties":{
    "schema":{"const":"env.resource_budget"},"id":{"type":"string","pattern":ID_PAT},
    "subject":{"type":"string"},"subject_kind":{"enum":["jurisdiction","city","ward","aquifer","airshed"]},
    "resource":{"enum":["aquifer","airshed","land","carbon","energy","waste","other"]},
    "capacity":{"type":"number"},"unit":{"type":"string"},          # e.g. MLD, tCO2/yr, ha, MW
    "regen_rate":Nn(),                                              # sustainable annual replenishment (same unit)
    "current_draw":Nn(),                                           # present consumption/extraction (same unit)
    "headroom":Nn(),                                               # regen_rate - current_draw (may be negative = overdrawn)
    "period":Sn(),"method":{"enum":["measured","modelled","estimated","declared"]},
    "observed_at":{"type":"string"},"confidence":Nn(),"source_ref":Sn(),"derived_from":An(),"prov":{"type":"object"},
    "tenant":Sn()}}
def advise():
    r,e=_post(NET+"/v1/schemas/advise",{"schema_id":"env.resource_budget","definition":BUDGET,"kind":"schema"})
    if e: print("advise ERR",e); return
    print("house_rule_violations:", r.get("house_rule_violations") or "none", "| rec:", r.get("recommendation"))
def govern():
    st,_=_post(IDN+"/realms/airawat/login",{"username":"steward"}); stoken=st["access_token"]
    cj=http.cookiejar.CookieJar(); _post(APP+"/login",{"realm":"airawatdeveloper","username":"devrick"},cj=cj)
    pr,pe=_post(APP+"/bridge/schema-request",{"schema_id":"env.resource_budget","version":"0.1.0","definition":BUDGET,"x_kind":"measure","justification":"ADR-0069 — environmental carrying-capacity budget (the sustainability constraint)."},cj=cj)
    if pe: print("propose ERR",pe); return
    dr,de=_post(NET+"/v1/schema-requests/decide",{"schema_id":"env.resource_budget","version":"0.1.0","decision":"approve"},token=stoken)
    print("env.resource_budget:", f"propose={pr.get('status')} approve={'OK '+dr['published_schema'] if dr else 'ERR '+str(de)}")
if __name__=="__main__":
    (govern if "--publish" in sys.argv else advise)()
