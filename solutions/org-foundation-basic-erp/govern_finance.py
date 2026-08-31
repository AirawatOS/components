import json, urllib.request, urllib.error, http.cookiejar
APP="http://localhost:8099"; NET="http://localhost:8090"; IDN="http://localhost:8093"
ID_PAT=r"^ds:[a-z0-9-]+:\S+$"
def post(url, body, token=None, cj=None):
    req=urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}, method="POST")
    if token: req.add_header("Authorization","Bearer "+token)
    op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(op.open(req,timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:220]}"
STOK=post(IDN+"/realms/airawat/login",{"username":"steward"})[0]["access_token"]
cj=http.cookiejar.CookieJar(); post(APP+"/login",{"realm":"airawatdeveloper","username":"devrick"},cj=cj)
S=lambda:{"type":"string"}; Sn=lambda:{"type":["string","null"]}; N=lambda:{"type":"number"}; Nn=lambda:{"type":["number","null"]}
def ent(sid,reqd,props,desc):
    p={"schema":{"const":sid},"id":{"type":"string","pattern":ID_PAT}}; p.update(props)
    return {"type":"object","x-kind":"entity","additionalProperties":False,"required":["schema","id"]+reqd,"properties":p,"title":sid,"description":desc}
SCHEMAS={
 # CHART OF ACCOUNTS — the GL backbone (double-entry). normal balance: asset/expense=debit, liability/equity/revenue=credit.
 "finance.account": ent("finance.account",["code","name","type"],{
    "code":S(),"name":S(),"type":{"enum":["asset","liability","equity","revenue","expense"]},
    "normal":{"enum":["debit","credit"]},"parent":Sn(),"tenant":Sn(),"created_at":Sn()},
    "A chart-of-accounts node (the GL backbone). Journal entries post debit/credit lines against accounts."),
 # JOURNAL ENTRY — balanced lines (Σdebit == Σcredit). Double-entry from day one; thin modules auto-build the 2 lines.
 "finance.transaction": ent("finance.transaction",["date","lines"],{
    "date":S(),"memo":Sn(),
    "lines":{"type":"array","items":{"type":"object"}},   # [{account, debit, credit}] — must balance (enforced at post)
    "amount":Nn(),                                        # convenience: the entry total (= Σdebit)
    "direction":Sn(),                                     # convenience tag: expenditure | revenue (for reporting)
    "budget":Sn(),                                        # optional finance.budget this entry charges
    "org_unit":Sn(),"source":Sn(),"source_kind":Sn(),    # provenance: the claim / PO / payroll run that produced it
    "status":{"enum":["committed","posted","void"]},
    "posted_by":Sn(),"tenant":Sn(),"created_at":Sn()},
    "A JOURNAL ENTRY: balanced debit/credit lines against accounts (double-entry). `committed` = a reservation (approved claim); `posted` = booked."),
 # BUDGET — a control overlay on an expense account for a fiscal year. available = allocated − committed − spent.
 "finance.budget": ent("finance.budget",["code","name","fiscal_year","allocated"],{
    "code":S(),"name":S(),"fiscal_year":S(),
    "account":Sn(),                                       # the finance.account (expense) this budget caps
    "org_unit":Sn(),"allocated":N(),"committed":Nn(),"spent":Nn(),
    "currency":Sn(),"status":{"enum":["active","closed"]},"tenant":Sn(),"created_at":Sn()},
    "A budget head — an allocation on an expense account for a fiscal year/unit. available = allocated − committed − spent."),
}
for sid,defn in SCHEMAS.items():
    pr,pe=post(APP+"/bridge/schema-request",{"schema_id":sid,"version":"0.1.0","definition":defn,"x_kind":"entity","justification":"Finance core (double-entry-ready ledger + budgets)."},cj=cj)
    if pe: print(f"  {sid}: PROPOSE {pe}"); continue
    dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":sid,"version":"0.1.0","decision":"approve"},token=STOK)
    print(f"  {sid}: {'OK '+dr['published_schema'] if dr else 'ERR '+str(de)}")
