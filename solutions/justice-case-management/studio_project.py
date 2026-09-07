import urllib.request,urllib.error,json,time
import os
IDN=os.environ.get("IDN","http://identity:8093"); K=os.environ.get("K","http://airawat-kernel:8080")
def login(realm,u):
    r=urllib.request.Request(f"{IDN}/realms/{realm}/login",data=json.dumps({"username":u}).encode(),headers={"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(r,timeout=10).read())["access_token"]
D=login("airawatdeveloper","devrick")
def sch(name,ver,origin): return {"ref":f"ds:airawat:schema/{name}@{ver}","kind":"schema","layer":"domain",
    "title":name,"origin":origin,"status":"published","reads":[],"writes":[]}
new=[("justice.hearing","0.1.0"),("justice.payment","0.1.0"),("justice.bail","0.1.0"),
     ("justice.adr_referral","0.1.0"),("justice.certified_copy","0.1.0")]
reused=[("justice.case","0.2.0"),("justice.filing","0.2.0"),("justice.scrutiny_result","0.2.0"),
        ("justice.court","0.2.0"),("justice.case_party","0.2.0"),("justice.appearance","0.2.0"),
        ("justice.evidence","0.2.0"),("justice.process_order","0.2.0"),("justice.process_service","0.2.0"),
        ("justice.disposal","0.2.0")]
members=[sch(n,v,"new") for n,v in new]+[sch(n,v,"existing") for n,v in reused]
members.append({"ref":"ds:airawatdeveloper:app/court-console","kind":"app","layer":"domain","title":"Court Console",
    "origin":"new","status":"published",
    "reads":["justice.case","justice.filing","justice.scrutiny_result","justice.hearing","justice.appearance","justice.disposal","justice.process_order","justice.process_service","justice.payment","justice.bail","justice.adr_referral","justice.certified_copy","platform.case","platform.task"],
    "writes":["justice.case","justice.filing","justice.scrutiny_result","justice.hearing","justice.appearance","justice.disposal","justice.process_order","justice.process_service","justice.payment","justice.bail","justice.adr_referral","justice.certified_copy","platform.case","platform.task","platform.inbox_item"]})
pid="ds:airawatdeveloper:studio/project/justice-case-management"
proj={"schema":"studio.project","id":pid,"tenant":"airawatdeveloper","title":"Court Case Management",
  "goal":"A court's full case lifecycle — file → scrutiny → registration → hearing → process → payment → bail → ADR → certified-copy → disposal — as governed, role-separated components. Live on tenant court-1.",
  "owner":"user://airawatdeveloper/devrick","status":"active",
  "target_solution":"ds:airawatdeveloper:solution/justice-case-management",
  "brief":{"users":["justice.advocate","justice.litigant","justice.scrutiny-officer","justice.registrar","justice.bench-clerk","justice.judge","justice.process-server","justice.mediator"],
    "entities":["case","filing","scrutiny_result","hearing","process_order","process_service","payment","bail","adr_referral","certified_copy","disposal","court","case_party"],
    "data_sources":["human-entered forms (user-initiated)"],
    "decisions":["scrutiny accept/return","registration","bail grant/reject","ADR referral","disposal"],
    "triggers":["form submit","inbox task"]},
  "members":members,"created_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
r=urllib.request.Request(f"{K}/v1/registry/objects",data=json.dumps({"id":pid,"object":proj}).encode(),
    method="POST",headers={"Content-Type":"application/json","Authorization":"Bearer "+D})
try:
    out=json.loads(urllib.request.urlopen(r,timeout=15).read())
    print("stored:",out.get("stored"),"wrote_by:",out.get("wrote_by"))
    print("members:",len(members))
except urllib.error.HTTPError as e:
    print("ERR",e.code,e.read().decode()[:300])
