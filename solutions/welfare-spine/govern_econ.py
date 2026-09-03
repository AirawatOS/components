#!/usr/bin/env python3
"""Govern `econ` domain + `econ.nightlight` (ADR-0069) — per-H3-cell night-time-lights radiance (VIIRS DNB), a
remote-sensing proxy for economic/energy intensity. The nightlight driver writes it; the deprivation-engine reads
it (÷ population = prosperity proxy → equity weight). Advise-only by default; --publish to govern.
Env AIROS_APP/AIROS_NET/AIROS_IDN (default local)."""
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
NIGHTLIGHT={
  "type":"object","x-kind":"measure","additionalProperties":False,
  "required":["schema","id","subject","observed_at","prov","avg_radiance"],
  "title":"econ.nightlight",
  "description":("Per-H3-cell night-time-lights radiance (VIIRS DNB, nW/cm2/sr) — a remote-sensing proxy for "
    "economic/energy intensity. subject = the cell h3. The deprivation-engine divides by population for a "
    "prosperity-per-capita signal. A MODELLED proxy (caveat: ~500m, dominated by non-residential light, saturates) "
    "— superseded by real income/consumption when available."),
  "properties":{
    "schema":{"const":"econ.nightlight"},"id":{"type":"string","pattern":ID_PAT},
    "subject":{"type":"string"},"subject_kind":{"enum":["cell"]},
    "space":{"type":["object","null"]},                          # canonical {h3,res,centroid} block
    "avg_radiance":{"type":"number"},                             # avg DNB radiance nW/cm2/sr
    "period":Sn(),                                               # e.g. "2024" or "2024-03"
    "method":{"enum":["remote","modelled","measured"]},
    "observed_at":{"type":"string"},"confidence":Nn(),"source_ref":Sn(),"derived_from":An(),"prov":{"type":"object"},
    "tenant":Sn()}}
def advise():
    r,e=_post(NET+"/v1/schemas/advise",{"schema_id":"econ.nightlight","definition":NIGHTLIGHT,"kind":"schema"})
    if e: print("advise ERR",e); return
    print("house_rule_violations:", r.get("house_rule_violations") or "none", "| rec:", r.get("recommendation"))
def govern():
    st,_=_post(IDN+"/realms/airawat/login",{"username":"steward"}); stoken=st["access_token"]
    cj=http.cookiejar.CookieJar(); _post(APP+"/login",{"realm":"airawatdeveloper","username":"devrick"},cj=cj)
    r,e=_post(NET+"/v1/domains",{"domain":"econ","label":"Economy"},token=stoken); print("domain econ:", ("OK" if r else "(exists/err) "+str(e)))
    pr,pe=_post(APP+"/bridge/schema-request",{"schema_id":"econ.nightlight","version":"0.1.0","definition":NIGHTLIGHT,"x_kind":"measure","justification":"ADR-0069 — nightlight economic proxy for the deprivation weight."},cj=cj)
    if pe: print("propose ERR",pe); return
    dr,de=_post(NET+"/v1/schema-requests/decide",{"schema_id":"econ.nightlight","version":"0.1.0","decision":"approve"},token=stoken)
    print("econ.nightlight:", f"propose={pr.get('status')} approve={'OK '+dr['published_schema'] if dr else 'ERR '+str(de)}")
if __name__=="__main__":
    (govern if "--publish" in sys.argv else advise)()
