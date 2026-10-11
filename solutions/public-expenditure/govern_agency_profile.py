import json, urllib.request, urllib.error
NET="http://network:8090"; KERNEL="http://airawat-kernel:8080"; IDN="http://identity:8093"
def post(u,b,tok):
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(u,data=json.dumps(b).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+tok},method="POST"),timeout=20).read() or b"{}"),None
    except urllib.error.HTTPError as e: return None,"%d %s"%(e.code,e.read().decode()[:200])
stok=json.loads(urllib.request.urlopen(urllib.request.Request(IDN+"/realms/airawat/login",data=b'{"username":"steward"}',headers={"Content-Type":"application/json"},method="POST"),timeout=10).read())["access_token"]
jtok=json.loads(urllib.request.urlopen(urllib.request.Request(IDN+"/realms/janaagraha/login",data=b'{"username":"janaagraha-dev","password":"Airawat@123"}',headers={"Content-Type":"application/json"},method="POST"),timeout=10).read())["access_token"]
NS=lambda:{"type":["string","null"]}; NAR=lambda it="string":{"type":["array","null"],"items":({"type":it} if isinstance(it,str) else it)}
ID={"type":"string","pattern":"^ds:[a-z0-9-]+:\\S+$"}
SOC={"type":"object","properties":{"platform":{"type":"string"},"url":{"type":"string"}},"additionalProperties":True}
defn={"type":"object","title":"platform.agency_profile","x-kind":"entity","required":["schema","id","display_name"],
 "properties":{"id":ID,"schema":{"const":"platform.agency_profile"},"tenant":NS(),
  "display_name":{"type":"string"},"legal_name":NS(),"agency_type":NS(),"jurisdiction":NS(),"parent_authority":NS(),
  "mandate":NS(),"about":NS(),"logo_url":NS(),"seal_url":NS(),"primary_color":NS(),"website":NS(),
  "email":NS(),"phone":NS(),"address":NS(),"languages":NAR("string"),"social":NAR(SOC),"established":NS(),"prov":{"type":["object","null"]}},
 "additionalProperties":False,"description":"An agency's institutional identity for its public presence (ADR-0111). Generic — any public agency."}
print("govern platform.agency_profile:", post(NET+"/v1/schemas",{"schema_id":"platform.agency_profile","version":"0.1.0","definition":defn,"sensitivity":"public"},stok))
# seed janaagraha
prof={"schema":"platform.agency_profile","id":"ds:janaagraha:platform/agency_profile/janaagraha","tenant":"janaagraha",
 "display_name":"Janaagraha","legal_name":"Janaagraha Centre for Citizenship and Democracy","agency_type":"Civil society organisation",
 "jurisdiction":"Bengaluru, Karnataka, India","mandate":"Strengthening citizen participation and the quality of urban governance.",
 "about":"Janaagraha works with citizens and governments to improve the quality of life in India's cities and towns. The Public Expenditure Dashboard opens up city public-works spending for citizen accountability.",
 "website":"https://www.janaagraha.org","email":"info@janaagraha.org","jurisdiction":"Bengaluru, Karnataka",
 "languages":["en","kn","hi"],"primary_color":"#0e2a47",
 "social":[{"platform":"twitter","url":"https://twitter.com/janaagraha"}],"established":"2001"}
print("seed janaagraha profile:", post(KERNEL+"/v1/registry/objects",{"id":prof["id"],"object":prof},jtok))
