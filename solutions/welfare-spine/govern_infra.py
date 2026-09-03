#!/usr/bin/env python3
"""Govern `infra.consumption` (ADR-0069) — the raw utility CONSUMPTION slot: metered/estimated use per
connection per billing period (kWh, kL, m3). This is the honest source that a real DISCOM/water-board/UPYOG
feed writes; a nightlight/model proxy writes the same slot with a lower confidence + method='modelled', so the
welfare interval widens honestly and tightens automatically when real metering arrives. A normaliser derives
welfare.service_level (per-capita) from this + household size.

`infra` domain already governed (welfare spine slice 1). Advise-only by default; --publish to govern.
Env: AIROS_APP/AIROS_NET/AIROS_IDN (default local; point at EC2 internal hostnames to govern there)."""
import os, sys, json, urllib.request, urllib.error, http.cookiejar
APP=os.environ.get("AIROS_APP","http://localhost:8099"); NET=os.environ.get("AIROS_NET","http://localhost:8090"); IDN=os.environ.get("AIROS_IDN","http://localhost:8093")
ID_PAT=r"^ds:[a-z0-9-]+:\S+$"
def _post(url, body, token=None, cj=None):
    req=urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}, method="POST")
    if token: req.add_header("Authorization","Bearer "+token)
    opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(opener.open(req,timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:220]}"
Nn=lambda:{"type":["number","null"]}; Sn=lambda:{"type":["string","null"]}; An=lambda:{"type":["array","null"]}
CONSUMPTION={
  "type":"object","x-kind":"measure","additionalProperties":False,
  "required":["schema","id","subject","observed_at","prov","quantity","unit"],
  "title":"infra.consumption",
  "description":("Raw utility consumption for one service connection over a billing/observation period — the honest "
    "metered source (kWh, kL, m3). Real DISCOM/water-board/UPYOG feeds write this with method='metered'; a "
    "nightlight/model proxy writes it with method='modelled' + low confidence. A normaliser derives welfare."
    "service_level (per-capita) from this + household size. subject = the service connection it was metered on."),
  "properties":{
    "schema":{"const":"infra.consumption"},"id":{"type":"string","pattern":ID_PAT},
    "subject":{"type":"string"},                                  # the infra.service_connection id metered
    "subject_kind":{"enum":["connection","household","building"]},
    "connection_ref":Sn(),                                        # explicit infra.service_connection id (= subject, usually)
    "capability":Sn(),                                            # welfare.capability id (water/energy/...)
    "quantity":{"type":"number"},"unit":{"type":"string"},        # kWh | kL | m3 | litres
    "period":{"type":["object","string","null"]},                # {start,end} or "2026-03"
    "method":{"enum":["metered","billed","estimated","modelled","declared"]},
    "observed_at":{"type":"string"},"confidence":Nn(),"derived_from":An(),"prov":{"type":"object"},
    "tenant":Sn()}}
def advise():
    r,e=_post(NET+"/v1/schemas/advise",{"schema_id":"infra.consumption","definition":CONSUMPTION,"kind":"schema"})
    if e: print("advise ERR",e); return False
    print("house_rule_violations:", r.get("house_rule_violations") or "none", "| rec:", r.get("recommendation")); return not (r.get("house_rule_violations") or [])
def govern():
    st,_=_post(IDN+"/realms/airawat/login",{"username":"steward"}); stoken=st["access_token"]
    cj=http.cookiejar.CookieJar(); _post(APP+"/login",{"realm":"airawatdeveloper","username":"devrick"},cj=cj)
    pr,pe=_post(APP+"/bridge/schema-request",{"schema_id":"infra.consumption","version":"0.1.0","definition":CONSUMPTION,"x_kind":"measure","justification":"ADR-0069 — raw utility consumption slot (real-data-ready)."},cj=cj)
    if pe: print("propose ERR",pe); return
    dr,de=_post(NET+"/v1/schema-requests/decide",{"schema_id":"infra.consumption","version":"0.1.0","decision":"approve"},token=stoken)
    print("infra.consumption:", f"propose={pr.get('status')} approve={'OK '+dr['published_schema'] if dr else 'ERR '+str(de)}")
if __name__=="__main__":
    (govern if "--publish" in sys.argv else advise)()
