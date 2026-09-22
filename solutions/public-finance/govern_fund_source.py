"""Public-finance fund-flow model — donor topologies + Programme Manager (fund-source slice F1).

Adds, in the existing governed `finance` domain:
  finance.fund_source  — the GOVERNED "fund source": one record per donor funding line, carrying funder,
                         funder_type, budget_modality (on/off-budget) and recipient_type. This is the
                         explicit taxonomy `hrt-summary` was waiting for ("best-effort until a governed
                         funding.source taxonomy exists"). It makes the two donor topologies data:
                           1) Development Partner → Sector Ministry → Region      (on_budget, e.g. SDG PF)
                           2) Development Partner → Implementing Partner (Off-Budget)  (off_budget)
  finance.fund_flow@0.2.0 / finance.advance@0.2.0 — additively gain an optional `fund_source` reference
                         (+ fund_flow keeps its channel enum for back-compat). No existing field changes.

And one governed actor:
  finance.programme-manager — a network.role at the sector ministry: oversees grants, verification, and
                         reporting against services delivered.

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

FUNDER_TYPE=["development_partner","government","own_revenue","ngo","private","other"]
BUDGET_MODALITY=["on_budget","off_budget"]
RECIPIENT_TYPE=["sector_ministry","region","implementing_partner","local_government","other"]

SCHEMAS={
 # NEW — the governed fund source (a donor funding line)
 "finance.fund_source": ("0.1.0", rec("finance.fund_source",["funder_type","budget_modality"],{
    "tenant":Sn(),
    "name":Sn(),                                             # the funding line label, e.g. "SDG PF — Window 1"
    "funder":Sn(),                                           # the specific funder, e.g. "World Bank" / "UNICEF"
    "funder_type":{"enum":FUNDER_TYPE},                      # who provides the money
    "budget_modality":{"enum":BUDGET_MODALITY},              # on_budget (via the government ledger) vs off_budget
    "recipient_type":{"enum":RECIPIENT_TYPE+[None]},         # who first receives it: ministry / region / implementing partner
    "programme":Sn(),                                        # the programme it funds, e.g. "SDG PF"
    "currency":Sn(),
    "external_ref":Sn(),                                     # the donor's grant / agreement id
    "steward_role":Sn(),                                     # the actor accountable (e.g. finance.programme-manager)
    "created_at":Sn(),"observed_at":Sn()},
    "A GOVERNED fund source: one donor/own-revenue funding line. `budget_modality` (on/off-budget) + "
    "`recipient_type` make the donor topology explicit — DP→Sector-Ministry→Region (on_budget) vs "
    "DP→Implementing-Partner (off_budget). fund_flow + advance reference this to roll up 'money by source'.")),
 # BUMP — additive: add an optional fund_source reference (all prior fields unchanged)
 "finance.fund_flow": ("0.2.0", rec("finance.fund_flow",["channel"],{
    "tenant":Sn(),"channel":{"enum":["government","grant","partner"]},
    "fund_source":Sn(),                                      # NEW → ds:…:finance/fund_source/… (the governed source)
    "project":Sn(),"program":Sn(),"budget":Sn(),"source":Sn(),"label":Sn(),
    "approved":Nn(),"allocated":Nn(),"released":Nn(),"committed":Nn(),"paid":Nn(),"expended":Nn(),
    "current_stage":Sn(),"bottleneck":Sn(),
    "history":{"type":"array","items":{"type":"object"}},
    "created_at":Sn(),"updated_at":Sn(),"observed_at":Sn()},
    "One fund's amounts across the lifecycle stages per channel, now referencing a governed finance.fund_source.")),
 "finance.advance": ("0.2.0", rec("finance.advance",["amount","status"],{
    "tenant":Sn(),"amount":N(),"to_party":Sn(),"purpose":Sn(),"memo":Sn(),
    "project":Sn(),"program":Sn(),"budget":Sn(),"source":Sn(),"source_kind":Sn(),"channel":Sn(),
    "fund_source":Sn(),                                      # NEW → the governed source this advance draws on
    "advance_account":Sn(),
    "disbursed_at":Sn(),"disbursed_by":Sn(),"disbursement_txn":Sn(),
    "utilized":Nn(),"liquidated":Nn(),"outstanding":Nn(),
    "status":{"enum":["disbursed","partially_liquidated","liquidated","reconciled"]},
    "liquidations":{"type":"array","items":{"type":"object"}},
    "created_at":Sn(),"observed_at":Sn()},
    "A disbursed ADVANCE tracked as a receivable, now referencing the governed finance.fund_source it draws on.")),
}

STOK=post(IDN+"/realms/airawat/login",{"username":"steward"})[0]["access_token"]
cj=http.cookiejar.CookieJar(); post(APP+"/login",{"realm":DEV_REALM,"username":DEV_USER},cj=cj)
print("== schemas ==")
for sid,(ver,defn) in SCHEMAS.items():
    pr,pe=post(APP+"/bridge/schema-request",{"schema_id":sid,"version":ver,"definition":defn,"x_kind":"record","justification":"Fund-flow model: governed fund source (donor topologies + on/off-budget); additive fund_source ref on flow/advance."},cj=cj)
    if pe: print(f"  {sid}@{ver}: PROPOSE {pe}"); continue
    dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":sid,"version":ver,"decision":"approve"},token=STOK)
    print(f"  {sid}@{ver}: {'OK '+dr.get('published_schema','') if dr else 'ERR '+str(de)}")
print("== role ==")
r,err=post(NET+"/v1/roles",{"role":"finance.programme-manager",
      "label":"Programme Manager",
      "definition":"Sector-ministry actor who oversees grants, performs verification, and reports against services delivered for a fund source."},token=STOK)
print("  finance.programme-manager:", ("OK "+r["published"]["id"]) if r else ("ERR "+str(err)))
kc,ke=post(KERNEL+"/v1/registry/schema-cache/refresh",{},token=STOK); print("schema-cache refresh:", kc or ke)
