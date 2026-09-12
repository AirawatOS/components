"""Public-finance fund-flow slice — govern two additive schemas in the existing `finance` domain:
  finance.advance    — a disbursed ADVANCE as a tracked receivable with a liquidation lifecycle
                       (disbursed → partially_liquidated → liquidated → reconciled). Powers HRT Scenario 3
                       + Indicator 4 (disbursement / utilization / OUTSTANDING balance).
  finance.fund_flow  — one fund's journey through named lifecycle stages per channel
                       (approved→allocated→released→committed→paid→expended). Powers HRT Scenario 1
                       (bottleneck: where is the money stuck?).
Propose as the developer session → approve as airawat steward → refresh the kernel schema cache.
Env: APP (app-host) NET (network) IDN (identity) KERNEL. Run inside app-host on EC2 with internal hostnames."""
import json, os, urllib.request, urllib.error, http.cookiejar
APP=os.environ.get("APP","http://localhost:8099"); NET=os.environ.get("NET","http://network:8090"); IDN=os.environ.get("IDN","http://identity:8093")
KERNEL=os.environ.get("KERNEL","http://kernel:8080"); DEV_REALM="airawatdeveloper"; DEV_USER="devrick"
ID_PAT=r"^ds:[a-z0-9-]+:\S+$"
def post(url, body, token=None, cj=None):
    req=urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}, method="POST")
    if token: req.add_header("Authorization","Bearer "+token)
    op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(op.open(req,timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:260]}"
S=lambda:{"type":"string"}; Sn=lambda:{"type":["string","null"]}; N=lambda:{"type":"number"}; Nn=lambda:{"type":["number","null"]}
def rec(sid,reqd,props,desc):
    p={"schema":{"const":sid},"id":{"type":"string","pattern":ID_PAT}}; p.update(props)
    return {"type":"object","x-kind":"record","additionalProperties":False,"required":["schema","id"]+reqd,"properties":p,"title":sid,"description":desc}

SCHEMAS={
 "finance.advance": ("0.1.0", rec("finance.advance",["amount","status"],{
    "tenant":Sn(),"amount":N(),"to_party":Sn(),"purpose":Sn(),"memo":Sn(),
    "project":Sn(),"program":Sn(),"budget":Sn(),"source":Sn(),"source_kind":Sn(),"channel":Sn(),
    "advance_account":Sn(),                                  # the Advances-Receivable (asset) GL account
    "disbursed_at":Sn(),"disbursed_by":Sn(),"disbursement_txn":Sn(),
    "utilized":Nn(),"liquidated":Nn(),"outstanding":Nn(),    # outstanding = amount − liquidated (the Indicator-4 number)
    "status":{"enum":["disbursed","partially_liquidated","liquidated","reconciled"]},
    "liquidations":{"type":"array","items":{"type":"object"}},   # [{amount, txn, proof_ref, at}]
    "created_at":Sn(),"observed_at":Sn()},
    "A disbursed ADVANCE tracked as a receivable until liquidated (settled against expenditure proof) and reconciled. outstanding = amount − liquidated — the unliquidated balance HRT tracks.")),
 "finance.fund_flow": ("0.1.0", rec("finance.fund_flow",["channel"],{
    "tenant":Sn(),"channel":{"enum":["government","grant","partner"]},
    "project":Sn(),"program":Sn(),"budget":Sn(),"source":Sn(),"label":Sn(),
    "approved":Nn(),"allocated":Nn(),"released":Nn(),"committed":Nn(),"paid":Nn(),"expended":Nn(),
    "current_stage":Sn(),"bottleneck":Sn(),                  # the first stage where money arrived but didn't move on
    "history":{"type":"array","items":{"type":"object"}},
    "created_at":Sn(),"updated_at":Sn(),"observed_at":Sn()},
    "One fund's amounts across the public-finance lifecycle stages (approved→allocated→released→committed→paid→expended), per channel. `bottleneck` = where it is stuck.")),
}
STOK=post(IDN+"/realms/airawat/login",{"username":"steward"})[0]["access_token"]
cj=http.cookiejar.CookieJar(); post(APP+"/login",{"realm":DEV_REALM,"username":DEV_USER},cj=cj)
for sid,(ver,defn) in SCHEMAS.items():
    pr,pe=post(APP+"/bridge/schema-request",{"schema_id":sid,"version":ver,"definition":defn,"x_kind":"record","justification":"HRT public-finance: advances/liquidation + multi-stage fund-flow (new, additive)."},cj=cj)
    if pe: print(f"  {sid}@{ver}: PROPOSE {pe}"); continue
    dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":sid,"version":ver,"decision":"approve"},token=STOK)
    print(f"  {sid}@{ver}: {'OK '+dr.get('published_schema','') if dr else 'ERR '+str(de)}")
kc,ke=post(KERNEL+"/v1/registry/schema-cache/refresh",{},token=STOK); print("  schema-cache refresh:", kc or ke)
