import json, urllib.request, urllib.error
IDN="http://identity:8093"; NET="http://network:8090"; APP="http://127.0.0.1:8099"
def post(u,b,token=None,cookie=None):
    h={"Content-Type":"application/json"}
    if token:h["Authorization"]="Bearer "+token
    if cookie:h["Cookie"]=cookie
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(u,data=json.dumps(b).encode(),headers=h,method="POST"),timeout=30).read() or b"{}"), None
    except urllib.error.HTTPError as e: return None,"%d %s"%(e.code,e.read().decode()[:200])
STOK=(post(IDN+"/realms/airawat/login",{"username":"steward"})[0] or {}).get("access_token")
_r=urllib.request.urlopen(urllib.request.Request(APP+"/login",data=json.dumps({"realm":"janaagraha","username":"janaagraha-dev","password":"Airawat@123"}).encode(),headers={"Content-Type":"application/json"},method="POST"),timeout=15)
CJ=(_r.headers.get("Set-Cookie") or "").split(";")[0]
NS=lambda:{"type":["string","null"]}; NN=lambda:{"type":["number","null"]}; NO=lambda:{"type":["object","null"]}
defn={"type":"object","title":"works.payment","x-kind":"record","x-subject":"work-payment",
  "required":["schema","id","work_order"],
  "properties":{"id":{"type":"string","pattern":"^ds:[a-z0-9-]+:\\S+$"},"schema":{"const":"works.payment"},"tenant":NS(),
    "work_order":{"type":"string"},"contract":NS(),"paid_net":NN(),"paid_gross":NN(),"net_vs_contract":NN(),
    "payment_flag":NS(),"paid_on":NS(),"transaction":NS(),"prov":NO(),"confidence":NN()},
  "additionalProperties":False,
  "description":"A payment recorded against a works.work_order — net/gross vs the contract value (x-subject work-payment, distinct from tax.payment)."}
pr,pe=post(APP+"/bridge/schema-request",{"schema_id":"works.payment","version":"0.1.0","definition":defn,"x_kind":"record",
           "justification":"PED work payment; x-subject work-payment to disambiguate from tax.payment (ADR-0060 §3a)."},cookie=CJ)
print("propose:", "ok" if pr else pe)
dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":"works.payment","version":"0.1.0","decision":"approve"},token=STOK)
print("decide:", "ok" if dr else de)
cat=json.loads(urllib.request.urlopen(NET+"/v1/schemas",timeout=12).read() or b"{}")
ids={(s.get('schema_id') or s.get('id')) for s in (cat.get('schemas') or cat)}
print("works.payment governed:", any("works.payment" in i for i in ids))
