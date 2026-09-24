import json, urllib.request, urllib.error, importlib.util, os
IDN="http://airawat-identity:8093"; KERNEL="http://kernel:8080"; TENANT="amritsar"
def login(r,u):
    q=urllib.request.Request(f"{IDN}/realms/{r}/login",data=json.dumps({"username":u}).encode(),headers={"Content-Type":"application/json"},method="POST")
    return json.loads(urllib.request.urlopen(q,timeout=15).read())["access_token"]
tok=login(TENANT,TENANT+"-admin")
def post(u,b):
    q=urllib.request.Request(u,data=json.dumps(b).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+tok},method="POST")
    return json.loads(urllib.request.urlopen(q,timeout=60).read() or b"{}")
def put(i,o): post(KERNEL+"/v1/registry/objects",{"id":i,"object":o})
# load campaign-engine handlers
spec=importlib.util.spec_from_file_location("ce","/tmp/ce_run.py"); ce=importlib.util.module_from_spec(spec); spec.loader.exec_module(ce)
# candidates
r=post(KERNEL+"/v1/registry/resolve",{"filter":{"schema":"tax.candidate"},"limit":100000})
CAND={ (o.get("object") or o)["id"]:(o.get("object") or o) for o in (r.get("results") or []) if (o.get("object") or o).get("id","").startswith(f"ds:{TENANT}:")}
print("candidates:",len(CAND))
STATE={}
def kget(i): return STATE.get(i) or CAND.get(i)
def kwrite(i,o): STATE[i]=o; put(i,o)
def ctx(body, sub="user://amritsar/tax-admin"):
    return {"tenant":TENANT,"sub":sub,"body":body,"kget":kget,"kwrite":kwrite,
            "claims":{"roles":["platform.tenant-admin"],"sub":sub}}
# 1) create campaign
res,e=ce.h_campaign_create(ctx({"name":"Amritsar Property-Tax Ground Truthing","code":"amr-ptgap-gt","description":"Field-verify AI-flagged unregistered properties","target_count":len(CAND)}))
assert not e, e; camp=res["campaign"]; print("campaign:",camp)
# 2) assign every candidate to its ward inspector
assigned=[]
for cid,c in CAND.items():
    ward=(c.get("ward") or "x"); officer=f"inspector-w{ward}"
    r2,e2=ce.h_campaign_assign(ctx({"candidate":cid,"officer":officer,"campaign":camp}))
    if e2: print("assign err",cid,e2); break
    assigned.append(r2["assignment"])
print("assigned:",len(assigned),"-> officers e.g.",assigned[0] if assigned else None)
# 3) prove the loop: officer records ground truth on ONE assignment
a0=assigned[0]; asg=STATE[a0]; officer=asg["assignee"]
r3,e3=ce.h_verify_submit(ctx({"assignment":a0,"outcome":"confirmed","built_area":120,"assessed_value":48000,"notes":"structure present, unregistered"}, sub=officer))
print("verify-submit:", e3 or r3)
# 4) counts
for s in ("campaign.campaign","campaign.assignment","platform.inbox_item","governance.decision"):
    rr=post(KERNEL+"/v1/registry/resolve",{"filter":{"schema":s},"limit":100000})
    n=sum(1 for o in (rr.get("results") or []) if (o.get("object") or o).get("id","").startswith(f"ds:{TENANT}:"))
    print(f"  {s}: {n}")
cand0=post(KERNEL+"/v1/registry/resolve",{"filter":{"schema":"tax.candidate"},"limit":100000})
from collections import Counter
print("  candidate statuses:", Counter((o.get('object') or o).get('status') for o in (cand0.get('results') or []) if (o.get('object') or o).get('id','').startswith('ds:amritsar:')))
