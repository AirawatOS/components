import json, urllib.request, urllib.error, http.cookiejar
APP="http://localhost:8099"; NET="http://localhost:8090"; IDN="http://localhost:8093"
ID_PAT=r"^ds:[a-z0-9-]+:\S+$"
def post(url, body, token=None, cj=None):
    req=urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}, method="POST")
    if token: req.add_header("Authorization","Bearer "+token)
    op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(op.open(req,timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:200]}"
STOK=post(IDN+"/realms/airawat/login",{"username":"steward"})[0]["access_token"]
cj=http.cookiejar.CookieJar(); post(APP+"/login",{"realm":"airawatdeveloper","username":"devrick"},cj=cj)
S=lambda:{"type":"string"}; Sn=lambda:{"type":["string","null"]}; Nn=lambda:{"type":["number","null"]}; On=lambda:{"type":["object","null"]}
def ent(sid,reqd,props,desc):
    p={"schema":{"const":sid},"id":{"type":"string","pattern":ID_PAT}}; p.update(props)
    return {"type":"object","x-kind":"entity","additionalProperties":False,"required":["schema","id"]+reqd,"properties":p,"title":sid,"description":desc}
SCHEMAS={
 "hr.leave_request": ent("hr.leave_request",["employee","from_date","to_date"],{
    "employee":S(),                                  # org.employee id of the applicant
    "manager":Sn(),                                  # org.employee id (approver) — from the applicant's reporting line
    "manager_login":Sn(),                            # the manager's user:// sub (routing target)
    "applicant_login":Sn(),                          # the applicant's user:// sub (for notify-back)
    "leave_type":{"enum":["casual","sick","earned","unpaid","other"]},
    "from_date":S(),"to_date":S(),"days":Nn(),"reason":Sn(),
    "status":{"enum":["pending","approved","rejected","cancelled"]},
    "decided_by":Sn(),"decided_at":Sn(),
    "scope":On(),                                    # the applicant's org scope (materialised) — scope-aware HR views
    "case":Sn(),"tenant":Sn(),"created_at":Sn()},
    "A staff member's request for leave, routed to their reporting manager for approval."),
 "hr.attendance": ent("hr.attendance",["employee","date"],{
    "employee":S(),"date":S(),                       # one record per employee per day
    "check_in":Sn(),"check_out":Sn(),
    "status":{"enum":["present","absent","on_leave","wfh","holiday"]},
    "tenant":Sn(),"created_at":Sn()},
    "A staff member's attendance for a day (check-in/out + status)."),
}
r,e=post(NET+"/v1/domains",{"domain":"hr","label":"Human Resources"},token=STOK); print("domain hr:", (r["published"]["id"] if r else e))
for sid,defn in SCHEMAS.items():
    pr,pe=post(APP+"/bridge/schema-request",{"schema_id":sid,"version":"0.1.0","definition":defn,"x_kind":"entity","justification":"Leave & Attendance module."},cj=cj)
    if pe: print(f"  {sid}: PROPOSE {pe}"); continue
    dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":sid,"version":"0.1.0","decision":"approve"},token=STOK)
    print(f"  {sid}: {'OK '+dr['published_schema'] if dr else 'ERR '+str(de)}")
