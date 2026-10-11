import json, urllib.request, urllib.error
NET="http://network:8090"; KERNEL="http://airawat-kernel:8080"; IDN="http://identity:8093"
def post(u,b,tok):
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(u,data=json.dumps(b).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+tok},method="POST"),timeout=20).read() or b"{}"),None
    except urllib.error.HTTPError as e: return None,"%d %s"%(e.code,e.read().decode()[:200])
stok=json.loads(urllib.request.urlopen(urllib.request.Request(IDN+"/realms/airawat/login",data=b'{"username":"steward"}',headers={"Content-Type":"application/json"},method="POST"),timeout=10).read())["access_token"]
jtok=json.loads(urllib.request.urlopen(urllib.request.Request(IDN+"/realms/janaagraha/login",data=b'{"username":"janaagraha-dev","password":"Airawat@123"}',headers={"Content-Type":"application/json"},method="POST"),timeout=10).read())["access_token"]
S=lambda t="string":{"type":t}; NS=lambda:{"type":["string","null"]}; NB=lambda:{"type":["boolean","null"]}; NO=lambda:{"type":["object","null"]}
NAR=lambda it="string":{"type":["array","null"],"items":({"type":it} if isinstance(it,str) else it)}
ID={"type":"string","pattern":"^ds:[a-z0-9-]+:\\S+$"}
OP={"type":"object","properties":{"op":{"type":"string"},"authorization":{"type":"string"},"consent_required":{"type":"boolean"}},"additionalProperties":True}
# ---- platform.capability (discovery INDEX; binds by reference) ----
cap={"type":"object","title":"platform.capability","x-kind":"entity","required":["schema","id","title","type","publisher"],
 "properties":{"id":ID,"schema":{"const":"platform.capability"},"tenant":NS(),
  "title":S(),"summary":NS(),"icon":NS(),"publisher":S(),
  "type":{"type":"string","enum":["information","service","disclosure","dataset","consultation","grievance","subscription","map","directory"]},
  "jurisdiction":NS(),"audience":NAR("string"),"languages":NAR("string"),"effective_from":NS(),"effective_until":NS(),
  "target_kind":{"type":["string","null"],"enum":["app","schema","service","publication","case","layer",None]},"target_ref":NS(),
  "operations":NAR(OP),"channels":NAR("string"),"keywords":NAR("string"),"topics":NAR("string"),"related":NAR("string"),
  "schema_org_type":NS(),"source":NS(),"approval":NS(),"provenance_contract":NB(),"prov":NO()},
 "additionalProperties":False,"description":"A public capability — the discovery INDEX of the Agency Public Interface (ADR-0111). References an existing record (app/schema/service/publication/case/layer); never a copy. The unit a web composer AND an AI agent discover."}
print("govern platform.capability:", post(NET+"/v1/schemas",{"schema_id":"platform.capability","version":"0.1.0","definition":cap,"sensitivity":"public"},stok))
# ---- platform.publication (approved public projection, not system of record) ----
pub={"type":"object","title":"platform.publication","x-kind":"entity","required":["schema","id","title","content_type","publisher"],
 "properties":{"id":ID,"schema":{"const":"platform.publication"},"tenant":NS(),
  "title":S(),"summary":NS(),"body":NS(),"publisher":S(),
  "content_type":{"type":"string","enum":["notice","circular","order","scheme","budget","rate","plan","eligibility","report","faq"]},
  "jurisdiction":NS(),"audience":NAR("string"),"language":NS(),"effective_from":NS(),"effective_until":NS(),
  "published_at":NS(),"review_due_at":NS(),"source":NS(),"approval":NS(),"version":NS(),
  "status":{"type":"string","enum":["draft","approved","published","superseded","archived"]},"prov":NO()},
 "additionalProperties":False,"description":"Governed public information = an APPROVED PROJECTION of a system-of-record fact (ADR-0111), with its own lifecycle/validity. Every channel (web/chat/AI) cites the same published version."}
print("govern platform.publication:", post(NET+"/v1/schemas",{"schema_id":"platform.publication","version":"0.1.0","definition":pub,"sensitivity":"public"},stok))

# ---- seed Janaagraha: 1 publication + 3 capabilities (real, bindable) ----
def write(o): return post(KERNEL+"/v1/registry/objects",{"id":o["id"],"object":o},jtok)
PUBLISHER="ds:janaagraha:platform/agency_profile/janaagraha"
publication={"schema":"platform.publication","id":"ds:janaagraha:platform/publication/ped-v1-open","tenant":"janaagraha",
 "title":"Public Expenditure Dashboard — now open to citizens","content_type":"notice","publisher":PUBLISHER,
 "summary":"BBMP public-works spending and procurement tenders are now open to any citizen with no sign-in: a ward map, 38,737 road tenders with competition flags, and downloadable data — every figure carrying its source.",
 "jurisdiction":"Bengaluru, Karnataka","audience":["citizen"],"language":"en","published_at":"2026-10-11","status":"published","version":"1",
 "source":"ds:janaagraha:app/public-expenditure-dashboard"}
print("seed publication:", write(publication))
caps=[
 {"schema":"platform.capability","id":"ds:janaagraha:platform/capability/public-expenditure","tenant":"janaagraha",
  "title":"Public Expenditure Dashboard","summary":"Ward-level BBMP public-works spending + procurement, open and cited.","icon":"🏗️",
  "publisher":PUBLISHER,"type":"dataset","jurisdiction":"Bengaluru, Karnataka","audience":["citizen","researcher"],"languages":["en"],
  "target_kind":"app","target_ref":"public-expenditure-dashboard","channels":["web","machine"],
  "operations":[{"op":"discover","authorization":"none","consent_required":False},{"op":"read","authorization":"none","consent_required":False}],
  "topics":["transparency","public-works","procurement","budget"],"keywords":["BBMP","spending","tenders","ward","roads"],
  "schema_org_type":"Dataset","source":"ds:janaagraha:app/public-expenditure-dashboard","provenance_contract":True},
 {"schema":"platform.capability","id":"ds:janaagraha:platform/capability/procurement-tenders","tenant":"janaagraha",
  "title":"BBMP road tenders (open data)","summary":"38,737 road procurement tenders with amounts, dates, bidders, documents and competition flags.","icon":"🧾",
  "publisher":PUBLISHER,"type":"dataset","jurisdiction":"Bengaluru, Karnataka","audience":["citizen","researcher","business"],"languages":["en"],
  "target_kind":"schema","target_ref":"procurement.tender","channels":["web","machine"],
  "operations":[{"op":"discover","authorization":"none","consent_required":False},{"op":"read","authorization":"none","consent_required":False}],
  "topics":["procurement","transparency"],"keywords":["tender","KPPP","eproc","single-bid"],
  "schema_org_type":"Dataset","source":"ds:janaagraha:driver/procurement-tenders","provenance_contract":True},
 {"schema":"platform.capability","id":"ds:janaagraha:platform/capability/ped-launch-notice","tenant":"janaagraha",
  "title":"Notice: PED now open","summary":"Public notice announcing the Public Expenditure Dashboard V1.","icon":"📣",
  "publisher":PUBLISHER,"type":"information","jurisdiction":"Bengaluru, Karnataka","audience":["citizen"],"languages":["en"],
  "target_kind":"publication","target_ref":"ds:janaagraha:platform/publication/ped-v1-open","channels":["web","machine"],
  "operations":[{"op":"discover","authorization":"none","consent_required":False},{"op":"read","authorization":"none","consent_required":False}],
  "topics":["notice"],"schema_org_type":"Article","source":"ds:janaagraha:platform/publication/ped-v1-open","provenance_contract":True},
]
for c in caps: print("seed capability", c["id"].split("/")[-1], ":", write(c))
