"""Govern the 7 new PED nouns so the extractor (and the other drivers) can write them.
Flow (proven, tax-gap-finder): register the `works` domain (steward) → propose each schema via
/bridge/schema-request (janaagraha-dev session) → approve via /v1/schema-requests/decide (airawat steward).
geo + procurement domains already exist. Schemas are network-canonical (ds:airawat:schema/<noun>@<ver>)."""
import json, urllib.request, urllib.error
IDN="http://identity:8093"; NET="http://network:8090"; APP="http://127.0.0.1:8099"
def post(u,b,token=None,cookie=None):
    h={"Content-Type":"application/json"}
    if token:h["Authorization"]="Bearer "+token
    if cookie:h["Cookie"]=cookie
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(u,data=json.dumps(b).encode(),headers=h,method="POST"),timeout=30).read() or b"{}"), None
    except urllib.error.HTTPError as e: return None, "%d %s"%(e.code,e.read().decode()[:160])

STOK=(post(IDN+"/realms/airawat/login",{"username":"steward"})[0] or {}).get("access_token")
# janaagraha-dev browser session (cookie) for the proposer bridge
_r=urllib.request.urlopen(urllib.request.Request(APP+"/login",data=json.dumps({"realm":"janaagraha","username":"janaagraha-dev","password":"Airawat@123"}).encode(),headers={"Content-Type":"application/json"},method="POST"),timeout=15)
CJ=(_r.headers.get("Set-Cookie") or "").split(";")[0]
print("steward token:",bool(STOK)," dev cookie:",bool(CJ))

def S(*t): return {"type": list(t) if len(t)>1 else t[0]}
NS=lambda: {"type":["string","null"]}; NN=lambda:{"type":["number","null"]}; NI=lambda:{"type":["integer","null"]}
NO=lambda: {"type":["object","null"]}; NAR=lambda it="string":{"type":["array","null"],"items":{"type":it}}
ID={"type":"string","pattern":"^ds:[a-z0-9-]+:\\S+$"}
def noun(nm, kind, req, props, desc):
    p={"id":ID,"schema":{"const":nm},"tenant":NS()}; p.update(props)
    return {"type":"object","title":nm,"x-kind":kind,"required":["schema","id"]+req,"properties":p,"additionalProperties":False,"description":desc}

SCHEMAS={
 "geo.road": noun("geo.road","entity",["road_id","street_name"],
   {"road_id":S("string"),"street_name":S("string"),"ward":NS(),"zone":NS(),"geometry":NO(),"osm_feature":NS()},
   "A named city street — the citizen's anchor in the Public Expenditure Dashboard; works are shown on it."),
 "geo.road_segment": noun("geo.road_segment","entity",["segment_id","road"],
   {"segment_id":S("string"),"road":S("string"),"segment_no":NI(),"sheet_index":NI(),"start_point":NO(),"end_point":NO(),
    "length_m":NN(),"from_landmark":NS(),"to_landmark":NS(),"geometry":NO(),"prov":NO(),"confidence":NN()},
   "A surveyed segment of a geo.road (start/end, length) — the unit a public work touches."),
 "works.contractor": noun("works.contractor","entity",["name"],
   {"name":S("string"),"reg_no":NS()},
   "A contractor (party) that executes public works."),
 "works.work_order": noun("works.work_order","entity",["work_order_no"],
   {"job_number":NS(),"work_order_no":S("string"),"work_order_date":NS(),"work_type":NS(),"other_work":NS(),
    "work_name":NS(),"significant_landmark":NS(),"sanctioned_amount":NN(),"sanctioned_year":NS(),"extension_days":NI(),
    "roads":NAR(),"n_segments_mapped":NI(),"mapped_len_m":NN(),"ward":NS(),"zone":NS(),"status":NS(),
    "completed_on":NS(),"final_cost":NN(),"saving":NN(),"completion_status":NS(),"completion_stated_figure":NN(),
    "prov":NO(),"confidence":NN()},
   "A public road work: sanction, type, the roads it touches, and completion — the core PED record."),
 "procurement.tender": noun("procurement.tender","entity",["work_order"],
   {"work_order":S("string"),"tender_indent":NS(),"tender_number":NS(),"notification_numbers":NAR(),
    "estimated_value":NN(),"published":NS(),"status":NS(),"source_link":NS(),"prov":NO(),"confidence":NN()},
   "The tender that preceded a works.work_order (from the state eProcurement portal)."),
 "works.contract": noun("works.contract","entity",["work_order"],
   {"work_order":S("string"),"tender":NS(),"contractor":NS(),"contract_value":NN(),"discount_pct":NN(),
    "method":NS(),"agreement_date":NS(),"dlp_from":NS(),"dlp_to":NS(),"dlp_status":NS(),"prov":NO(),"confidence":NN()},
   "The awarded contract for a works.work_order: value, agreement, defect-liability period."),
 "works.payment": noun("works.payment","record",["work_order"],
   {"work_order":S("string"),"contract":NS(),"paid_net":NN(),"paid_gross":NN(),"net_vs_contract":NN(),
    "payment_flag":NS(),"paid_on":NS(),"transaction":NS(),"prov":NO(),"confidence":NN()},
   "A payment recorded against a works.work_order — net/gross vs the contract value."),
}

print("\n1) register the `works` domain")
r,e=post(NET+"/v1/domains",{"domain":"works","label":"Public Works"},token=STOK)
print("   works domain:", "OK" if r else "(exists?) "+str(e))

print("\n2) propose + approve each schema")
for nm,defn in SCHEMAS.items():
    pr,pe=post(APP+"/bridge/schema-request",{"schema_id":nm,"version":"0.1.0","definition":defn,"x_kind":defn["x-kind"],
               "justification":"Public Expenditure Dashboard (Janaagraha) — governed PED data model, ADR-0109."},cookie=CJ)
    dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":nm,"version":"0.1.0","decision":"approve"},token=STOK)
    print(f"   {nm:22s} propose:{'ok' if pr else pe}  decide:{'ok' if dr else de}")

print("\n3) verify governed")
import urllib.request as u
cat=json.loads(u.urlopen(NET+"/v1/schemas",timeout=12).read() or b"{}")
ids={ (s.get('schema_id') or s.get('id')) for s in (cat.get('schemas') or cat) }
for nm in SCHEMAS: print(f"   {nm:22s}:", "GOVERNED ✓" if any(nm in i for i in ids) else "MISSING")
