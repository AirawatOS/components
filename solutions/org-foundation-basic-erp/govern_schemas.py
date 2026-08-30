#!/usr/bin/env python3
"""Org Foundation Slice 1 — govern `org` domain + propose/approve the 5 backbone schemas.
Propose as devrick (app_host /bridge/schema-request); approve as steward (network /v1/schema-requests/decide)."""
import json, urllib.request, urllib.error, http.cookiejar

APP = "http://localhost:8099"      # app_host (Studio bridges)
NET = "http://localhost:8090"      # network directory (governance)
IDN = "http://localhost:8093"      # identity

ID_PAT = r"^ds:[a-z0-9-]+:\S+$"

def _post(url, body, token=None, cj=None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    if token: req.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try:
        return json.loads(opener.open(req, timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code} {e.read().decode()[:200]}"

# --- creds -----------------------------------------------------------------
steward, _ = _post(IDN + "/realms/airawat/login", {"username": "steward"})
STOKEN = steward["access_token"]
cj = http.cookiejar.CookieJar()
_post(APP + "/login", {"realm": "airawatdeveloper", "username": "devrick"}, cj=cj)   # devrick session cookie

# --- helpers ---------------------------------------------------------------
def ent(schema_id, required, props):
    """Build a house-rule-clean ENTITY schema (R1 object, R2 schema+id, R2b id pattern, R3 closed, R4 typed)."""
    p = {"schema": {"const": schema_id}, "id": {"type": "string", "pattern": ID_PAT}}
    p.update(props)
    return {"type": "object", "x-kind": "entity", "additionalProperties": False,
            "required": ["schema", "id"] + required, "properties": p,
            "title": schema_id, "description": DESC[schema_id]}

S = lambda: {"type": "string"}
Sn = lambda: {"type": ["string", "null"]}
On = lambda: {"type": ["object", "null"]}
Nn = lambda: {"type": ["number", "null"]}

DESC = {
 "org.org_unit": "A node in the organisation hierarchy (department / division / team / office). Self-referential parent chain gives variable depth.",
 "org.area":     "A node in the organisation's internal geographic hierarchy (zone / ward / locality / street). Variable depth via parent; optionally anchored to a federated network.jurisdiction city.",
 "org.employee": "A person employed by the organisation (staff record). Optionally linked to a citizen person-ref.",
 "org.posting":  "An assignment placing an employee in an org unit + area, with a role and designation. The (employee x unit x area x role) tuple.",
 "org.goal":     "An objective/target assigned to an org unit or an employee, tracked over a period.",
}

SCHEMAS = {
 "org.org_unit": ent("org.org_unit", ["code", "name"], {
    "code": S(), "name": S(),
    "kind": {"enum": ["department", "division", "team", "office", "unit", "board"]},
    "parent": Sn(),           # parent org_unit id/code -> the hierarchy
    "area": Sn(),             # org.area code this unit operates in
    "jurisdiction": Sn(),     # network.jurisdiction code (federated geo anchor)
    "head": Sn(),             # org.employee id of the unit head
    "tenant": Sn(), "created_at": Sn()}),

 "org.area": ent("org.area", ["code", "name", "level"], {
    "code": S(), "name": S(),
    "level": S(),             # zone | ward | locality | street | ... (variable-depth label)
    "parent": Sn(),           # parent area code -> the hierarchy
    "jurisdiction": Sn(),     # network.jurisdiction city code this area sits under
    "geometry": On(),         # optional GeoJSON boundary
    "tenant": Sn(), "created_at": Sn()}),

 "org.employee": ent("org.employee", ["emp_code", "name"], {
    "emp_code": S(), "name": S(),
    "citizen": Sn(),          # person-ref (user://... or civic.profile citizen) if the employee is also a citizen
    "contact": On(),          # {email, phone}
    "grade": Sn(),
    "manager": Sn(),          # org.employee id of the reporting manager
    "org_unit": Sn(),         # primary org_unit id
    "status": {"enum": ["active", "on_leave", "separated"]},
    "tenant": Sn(), "created_at": Sn()}),

 "org.posting": ent("org.posting", ["employee", "org_unit"], {
    "employee": S(),          # org.employee id
    "org_unit": S(),          # org.org_unit id
    "area": Sn(),             # org.area code
    "role": Sn(),             # governed network role id (e.g. civic.clerk)
    "designation": Sn(),      # free-text designation/grade title
    "reporting_to": Sn(),     # org.employee id
    "from_date": Sn(), "to_date": Sn(),
    "status": {"enum": ["active", "ended"]},
    "tenant": Sn(), "created_at": Sn()}),

 "org.goal": ent("org.goal", ["title", "owner"], {
    "title": S(),
    "owner": S(),             # org_unit id or employee id
    "owner_kind": {"enum": ["org_unit", "employee"]},
    "metric": Sn(),
    "target": {"type": ["number", "string", "null"]},
    "progress": Nn(),
    "period": Sn(),           # FY2026 / Q1-2026 / etc.
    "status": {"enum": ["draft", "active", "met", "missed"]},
    "tenant": Sn(), "created_at": Sn()}),
}

# --- 1) govern the `org` domain -------------------------------------------
r, err = _post(NET + "/v1/domains", {"domain": "org", "label": "Organization"}, token=STOKEN)
print("domain org:", ("OK " + r["published"]["id"]) if r else ("ERR " + str(err)))

# --- 2) propose (devrick) -> approve (steward) each schema -----------------
for sid, defn in SCHEMAS.items():
    pr, perr = _post(APP + "/bridge/schema-request",
                     {"schema_id": sid, "version": "0.1.0", "definition": defn,
                      "x_kind": "entity", "justification": "Org Foundation slice 1 — organisational backbone."},
                     cj=cj)
    if perr:
        print(f"  {sid}: PROPOSE ERR {perr}"); continue
    dr, derr = _post(NET + "/v1/schema-requests/decide",
                     {"schema_id": sid, "version": "0.1.0", "decision": "approve"}, token=STOKEN)
    print(f"  {sid}: propose={pr.get('status')} approve={'OK '+dr['published_schema'] if dr else 'ERR '+str(derr)}")
