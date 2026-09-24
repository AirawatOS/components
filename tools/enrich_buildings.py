import json, urllib.request
K="http://kernel:8080"; IDN="http://airawat-identity:8093"; T="amritsar"
def login(r,u):
    q=urllib.request.Request(f"{IDN}/realms/{r}/login",data=json.dumps({"username":u}).encode(),headers={"Content-Type":"application/json"},method="POST")
    return json.loads(urllib.request.urlopen(q,timeout=15).read())["access_token"]
tok=login(T,T+"-admin")
def res(s):
    q=urllib.request.Request(K+"/v1/registry/resolve",data=json.dumps({"filter":{"schema":s},"limit":100000}).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+tok},method="POST")
    return [x.get("object") or x for x in (json.loads(urllib.request.urlopen(q,timeout=120).read()).get("results") or []) if (x.get("object") or x).get("id","").startswith("ds:"+T+":")]
def put(i,o):
    q=urllib.request.Request(K+"/v1/registry/objects",data=json.dumps({"id":i,"object":o}).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+tok},method="POST")
    urllib.request.urlopen(q,timeout=30).read()
parcels={p.get("parcel_id"):p for p in res("geo.parcel")}
assess={a.get("uid"):a for a in res("tax.assessment")}
cands={c.get("parcel_id"):c for c in res("tax.candidate")}
blds=[b for b in res("geo.building") if "/tax-" in b.get("id","")]
n=0
for b in blds:
    uid=b["id"].split("/tax-",1)[1]
    p=parcels.get(uid) or {}; a=assess.get(uid) or {}; c=cands.get(uid)
    status="unregistered" if b.get("method")=="tax-unregistered" else "registered"
    attrs={"owner":p.get("owner") or a.get("owner"), "uid":uid, "property_id":uid, "ward":p.get("ward") or a.get("ward"),
           "mobile":a.get("mobile"), "land_use":p.get("land_use"), "tax_status":status,
           "confidence":(c or {}).get("confidence"), "record":(c or {}).get("id") or p.get("id")}
    b["prov"]=dict(b.get("prov") or {}, **{k:v for k,v in attrs.items() if v is not None})
    put(b["id"],b); n+=1
print(f"enriched {n} buildings with prov.{{owner,uid,property_id,ward,mobile,land_use,tax_status,confidence,record}}")
print("sample prov:", {k:blds[0]["prov"].get(k) for k in ("owner","uid","property_id","ward","tax_status","confidence")})
