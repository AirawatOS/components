"""Org Foundation — Payroll: govern hr.compensation (the REAL cost source for project apportionment).

Labour cost is apportioned to projects by the payroll-engine: cost = hr.compensation.monthly_cost × project.allocation.percent,
posted as a project-tagged finance.transaction. NO fabrication — an allocated employee without a compensation record is
reported skipped, never guessed. Effective-dated so pay can change over time.
"""
import json, urllib.request, urllib.error, http.cookiejar
APP="http://localhost:8099"; NET="http://localhost:8090"; IDN="http://localhost:8093"
ID_PAT=r"^ds:[a-z0-9-]+:\S+$"
def post(url, body, token=None, cj=None):
    req=urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}, method="POST")
    if token: req.add_header("Authorization","Bearer "+token)
    op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(op.open(req,timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:240]}"
STOK=post(IDN+"/realms/airawat/login",{"username":"steward"})[0]["access_token"]
cj=http.cookiejar.CookieJar(); post(APP+"/login",{"realm":"airawatdeveloper","username":"devrick"},cj=cj)
S=lambda:{"type":"string"}; Sn=lambda:{"type":["string","null"]}; N=lambda:{"type":"number"}
d={"type":"object","x-kind":"record","additionalProperties":False,
   "required":["schema","id","employee","monthly_cost"],
   "properties":{"schema":{"const":"hr.compensation"},"id":{"type":"string","pattern":ID_PAT},
     "employee":S(),"monthly_cost":N(),"currency":Sn(),
     "effective_from":Sn(),"effective_to":Sn(),                # effective-dated: latest active row before the run period wins
     "status":{"enum":["active","ended"]},"tenant":Sn(),"created_at":Sn()},
   "title":"hr.compensation",
   "description":"An employee's monthly cost-to-org (the basis for apportioning labour to projects). Effective-dated; the latest active row on/before the payroll period applies."}
pr,pe=post(APP+"/bridge/schema-request",{"schema_id":"hr.compensation","version":"0.1.0","definition":d,"x_kind":"record","justification":"Real cost source for payroll → project apportionment."},cj=cj)
if pe: print("hr.compensation: PROPOSE",pe)
else:
    dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":"hr.compensation","version":"0.1.0","decision":"approve"},token=STOK)
    print("hr.compensation:", ('OK '+dr['published_schema']) if dr else ('ERR '+str(de)))
