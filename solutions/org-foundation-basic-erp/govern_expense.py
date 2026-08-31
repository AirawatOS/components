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
S=lambda:{"type":"string"}; Sn=lambda:{"type":["string","null"]}; N=lambda:{"type":"number"}; On=lambda:{"type":["object","null"]}
defn={"type":"object","x-kind":"entity","additionalProperties":False,"title":"hr.expense_claim",
  "description":"A staff member's expense reimbursement claim, routed to their manager. On approval it settles into the ledger (finance-post) against a budget, gated by the approver's expense approval_limit policy.",
  "required":["schema","id","employee","amount"],
  "properties":{"schema":{"const":"hr.expense_claim"},"id":{"type":"string","pattern":ID_PAT},
    "employee":S(),"applicant_login":Sn(),"manager":Sn(),"manager_login":Sn(),
    "category":Sn(),"amount":N(),"currency":Sn(),"description":Sn(),"incurred_on":Sn(),
    "budget":Sn(),"status":{"enum":["pending","approved","rejected","paid","cancelled"]},
    "decided_by":Sn(),"decided_at":Sn(),"finance_txn":Sn(),
    "scope":On(),"case":Sn(),"tenant":Sn(),"created_at":Sn()}}
pr,pe=post(APP+"/bridge/schema-request",{"schema_id":"hr.expense_claim","version":"0.1.0","definition":defn,"x_kind":"entity","justification":"Expense module (settles to finance)."},cj=cj)
print("propose:", pr or pe)
dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":"hr.expense_claim","version":"0.1.0","decision":"approve"},token=STOK)
print("approve:", dr or de)
