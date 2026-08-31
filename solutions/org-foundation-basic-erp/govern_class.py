import json, urllib.request, urllib.error, http.cookiejar
APP="http://localhost:8099"; NET="http://localhost:8090"; IDN="http://localhost:8093"
ID_PAT=r"^ds:[a-z0-9-]+:\S+$"
def post(url, body, token=None, cj=None):
    req=urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}, method="POST")
    if token: req.add_header("Authorization","Bearer "+token)
    op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(op.open(req,timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:200]}"
def resolve(sid,ver):
    r=urllib.request.Request(NET+"/v1/schemas/resolve", data=json.dumps({"schema_id":sid,"version":ver}).encode(), headers={"Content-Type":"application/json"}, method="POST")
    d=json.loads(urllib.request.urlopen(r,timeout=10).read()); return d.get("definition") or d
STOK=post(IDN+"/realms/airawat/login",{"username":"steward"})[0]["access_token"]
cj=http.cookiejar.CookieJar(); post(APP+"/login",{"realm":"airawatdeveloper","username":"devrick"},cj=cj)
S=lambda:{"type":"string"}; Sn=lambda:{"type":["string","null"]}; Nn=lambda:{"type":["number","null"]}
def ent(sid,reqd,props,desc):
    p={"schema":{"const":sid},"id":{"type":"string","pattern":ID_PAT}}; p.update(props)
    return {"type":"object","x-kind":"entity","additionalProperties":False,"required":["schema","id"]+reqd,"properties":p,"title":sid,"description":desc}
SCHEMAS={
 "hr.job_level": ent("hr.job_level",["code","name","rank"],{
    "code":S(),"name":S(),"rank":{"type":"number"},   # rank = orderable seniority (1=lowest); policy uses >=
    "description":Sn(),"tenant":Sn(),"created_at":Sn()},
    "A grade / band in the org hierarchy of seniority. `rank` orders levels so policy can say 'level >= N'."),
 "hr.career_stream": ent("hr.career_stream",["code","name"],{
    "code":S(),"name":S(),"description":Sn(),"tenant":Sn(),"created_at":Sn()},
    "A job family / career track (e.g. Administrative, Technical, Health, Finance)."),
 "org.position": ent("org.position",["code","title"],{
    "code":S(),"title":S(),
    "stream":Sn(),                                    # hr.career_stream code
    "level":Sn(),                                     # hr.job_level code
    "org_unit":Sn(),                                  # org.org_unit id the position sits in
    "eligible_roles":{"type":"array","items":{"type":"string"}},  # governed role ids this position may be granted
    "reports_to":Sn(),                               # org.position id (reporting line at the SLOT level)
    "headcount":Nn(),"jurisdiction":Sn(),"tenant":Sn(),"created_at":Sn()},
    "A job SLOT = stream x level within an org unit, with the roles it's eligible for and its reporting line. Postings FILL a position with a person; goals/policy/pay attach here."),
}
r,e=post(NET+"/v1/domains",{"domain":"hr","label":"Human Resources"},token=STOK)  # idempotent
for sid,defn in SCHEMAS.items():
    pr,pe=post(APP+"/bridge/schema-request",{"schema_id":sid,"version":"0.1.0","definition":defn,"x_kind":"entity","justification":"Classification layer: job level x stream x position."},cj=cj)
    if pe: print(f"  {sid}: PROPOSE {pe}"); continue
    dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":sid,"version":"0.1.0","decision":"approve"},token=STOK)
    print(f"  {sid}: {'OK '+dr['published_schema'] if dr else 'ERR '+str(de)}")
# bump org.posting@0.2.0 with optional `position`
pd=dict(resolve("org.posting","0.1.0")); props=dict(pd.get("properties") or {}); props["position"]=Sn(); pd["properties"]=props
pr=urllib.request.Request(NET+"/v1/schemas", data=json.dumps({"schema_id":"org.posting","version":"0.2.0","definition":pd}).encode(), headers={"Content-Type":"application/json"}, method="POST")
print("org.posting@0.2.0:", json.loads(urllib.request.urlopen(pr,timeout=10).read()).get("published"))
