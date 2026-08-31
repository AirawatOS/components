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
Sn=lambda:{"type":["string","null"]}; Nn=lambda:{"type":["number","null"]}; On=lambda:{"type":["object","null"]}
defn={"type":"object","x-kind":"entity","additionalProperties":False,
  "title":"org.policy",
  "description":"A tenant POLICY — one place to govern entitlements/limits for a domain (leave, travel, expense, attendance...). `applies_to` targets whom by seniority (min/max rank), stream, role, unit; `rules` is the domain-specific payload; most-specific match wins at resolve.",
  "required":["schema","id","name","domain"],
  "properties":{
    "schema":{"const":"org.policy"},
    "id":{"type":"string","pattern":ID_PAT},
    "name":{"type":"string"},
    "domain":{"type":"string"},                          # leave | travel | expense | attendance | ...
    "applies_to":On(),                                   # {min_rank?, max_rank?, stream?, role?, org_unit?, jurisdiction?}
    "rules":On(),                                         # domain-specific payload (leave: {leave_type, annual_entitlement, accrual_per_month, carry_forward_max}; travel: {class, per_diem}; expense: {approval_limit})
    "priority":Nn(),                                     # optional manual tie-break (higher wins)
    "status":{"enum":["active","draft","retired"]},
    "effective_from":Sn(),"tenant":Sn(),"created_at":Sn()}}
pr,pe=post(APP+"/bridge/schema-request",{"schema_id":"org.policy","version":"0.1.0","definition":defn,"x_kind":"entity","justification":"Unified policy engine (leave/travel/expense/...)."},cj=cj)
print("propose:", pr or pe)
dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":"org.policy","version":"0.1.0","decision":"approve"},token=STOK)
print("approve:", dr or de)
