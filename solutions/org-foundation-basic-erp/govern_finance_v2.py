"""Finance schema v0.2.0 — additive, backward-compatible fields for Finance Console 0.4.0:
  finance.account      + subtype (chart sub-classification: Direct/Indirect expense, Bank & Cash, …)
  finance.transaction  + voucher_type, party, invoice_ref, invoice_url (voucher entry + invoice link)
Files the change-request (as the developer session) then approves it (airawat steward), exactly like
govern_finance.py. Env: APP (app-host), NET (network), IDN (identity). Run inside app-host on EC2 with
APP=http://localhost:8099 NET=http://network:8090 IDN=http://identity:8093, or locally with the defaults."""
import json, os, urllib.request, urllib.error, http.cookiejar
APP=os.environ.get("APP","http://localhost:8099"); NET=os.environ.get("NET","http://localhost:8090"); IDN=os.environ.get("IDN","http://localhost:8093")
DEV_REALM=os.environ.get("DEV_REALM","airawatdeveloper"); DEV_USER=os.environ.get("DEV_USER","devrick")
ID_PAT=r"^ds:[a-z0-9-]+:\S+$"
# per-schema target version (transaction 0.2.0 already existed from the project-layer work → bump to 0.3.0)
VERS={"finance.account":"0.2.0","finance.transaction":"0.3.0"}

def post(url, body, token=None, cj=None):
    req=urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}, method="POST")
    if token: req.add_header("Authorization","Bearer "+token)
    op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(op.open(req,timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:240]}"

S=lambda:{"type":"string"}; Sn=lambda:{"type":["string","null"]}; N=lambda:{"type":"number"}; Nn=lambda:{"type":["number","null"]}
def ent(sid,reqd,props,desc):
    p={"schema":{"const":sid},"id":{"type":"string","pattern":ID_PAT}}; p.update(props)
    return {"type":"object","x-kind":"entity","additionalProperties":False,"required":["schema","id"]+reqd,"properties":p,"title":sid,"description":desc}

SCHEMAS={
 "finance.account": ent("finance.account",["code","name","type"],{
    "code":S(),"name":S(),"type":{"enum":["asset","liability","equity","revenue","expense"]},
    "subtype":Sn(),                                       # chart sub-classification (e.g. Direct/Indirect expense)
    "normal":{"enum":["debit","credit"]},"parent":Sn(),"tenant":Sn(),"created_at":Sn()},
    "A chart-of-accounts node (the GL backbone). `subtype` sub-classifies within the type (Direct/Indirect expense, Bank & Cash, Accounts Payable, …)."),
 "finance.transaction": ent("finance.transaction",["date","lines"],{
    "date":S(),"memo":Sn(),"lines":{"type":"array","items":{"type":"object"}},
    "amount":Nn(),"direction":Sn(),"budget":Sn(),"org_unit":Sn(),"source":Sn(),"source_kind":Sn(),
    "voucher_type":Sn(),"party":Sn(),"invoice_ref":Sn(),"invoice_url":Sn(),   # voucher entry + invoice link
    "project":Sn(),                                       # (kept — payroll apportionment tags the project)
    "status":{"enum":["committed","posted","void"]},"posted_by":Sn(),"tenant":Sn(),"created_at":Sn()},
    "A JOURNAL ENTRY: balanced debit/credit lines (double-entry). `voucher_type` (payment|receipt|contra|journal|sales|purchase) + `party` + `invoice_ref`/`invoice_url` capture voucher context."),
}

STOK=post(IDN+"/realms/airawat/login",{"username":"steward"})[0]["access_token"]
cj=http.cookiejar.CookieJar(); post(APP+"/login",{"realm":DEV_REALM,"username":DEV_USER},cj=cj)
for sid,defn in SCHEMAS.items():
    ver=VERS[sid]
    pr,pe=post(APP+"/bridge/schema-request",{"schema_id":sid,"version":ver,"definition":defn,"x_kind":"entity","justification":"Finance Console 0.4.0: sub-types + voucher/invoice fields (additive)."},cj=cj)
    if pe: print(f"  {sid}@{ver}: PROPOSE {pe}"); continue
    dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":sid,"version":ver,"decision":"approve"},token=STOK)
    print(f"  {sid}@{ver}: {'OK '+dr['published_schema'] if dr else 'ERR '+str(de)}")
# make the new versions take effect immediately (drop the kernel's resolved-schema cache — needs a steward token)
kc,ke=post(os.environ.get("KERNEL","http://kernel:8080")+"/v1/registry/schema-cache/refresh",{},token=STOK)
print("  schema-cache refresh:", kc or ke)
