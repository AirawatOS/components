import json, urllib.request, urllib.error, http.cookiejar
APP="http://localhost:8099"; NET="http://localhost:8090"; IDN="http://localhost:8093"
ID_PAT=r"^ds:[a-z0-9-]+:\S+$"
def _post(url, body, token=None, cj=None):
    req=urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}, method="POST")
    if token: req.add_header("Authorization","Bearer "+token)
    op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(op.open(req,timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:250]}"
STOKEN=_post(IDN+"/realms/airawat/login",{"username":"steward"})[0]["access_token"]
cj=http.cookiejar.CookieJar(); _post(APP+"/login",{"realm":"airawatdeveloper","username":"devrick"},cj=cj)
defn={"type":"object","x-kind":"record","additionalProperties":False,
  "title":"governance.role_grant",
  "description":"Binds a governed ROLE to a person (subject), optionally scoped to an org unit / area / jurisdiction. A user's EFFECTIVE roles = identity-token roles UNION active (non-revoked) role_grants. Emitted by an authority — a tenant admin assigning a role, or an org.posting projected to a grant (source).",
  "required":["schema","id","subject","role"],
  "properties":{
    "schema":{"const":"governance.role_grant"},
    "id":{"type":"string","pattern":ID_PAT},
    "subject":{"type":"string"},
    "role":{"type":"string"},
    "tenant":{"type":["string","null"]},
    "granted_by":{"type":["string","null"]},
    "granted_at":{"type":["string","null"]},
    "revoked":{"type":"boolean"},
    "scope":{"type":["object","null"]},
    "source":{"type":["string","null"]}}}
pr,perr=_post(APP+"/bridge/schema-request",{"schema_id":"governance.role_grant","version":"0.1.0","definition":defn,"x_kind":"record","justification":"Formalise the RBAC grant read by _effective_roles; add org_unit/area scope + source (posting projection)."},cj=cj)
print("propose:", pr or perr)
dr,derr=_post(NET+"/v1/schema-requests/decide",{"schema_id":"governance.role_grant","version":"0.1.0","decision":"approve"},token=STOKEN)
print("approve:", dr or derr)
