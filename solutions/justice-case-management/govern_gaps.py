#!/usr/bin/env python3
"""Justice Case Management — Slice 1 govern the GAPS on top of the existing signed
`ds:airawat:pack/justice@0.3.0` (10 schemas + 3 roles + justice.file_case interface).

Adds:
  - justice.hearing@0.1.0  (the 'second root' — the scheduled session; xlsx Sheet3/Sheet5)
  - roles: justice.judge, justice.bench-clerk, justice.scrutiny-officer

Reuses (NO new versions): justice.case/filing/scrutiny_result/court/case_party/appearance/
evidence/process_order/process_service/disposal @0.2.0, roles justice.advocate/litigant/registrar,
and the generic spine platform.case/task/inbox_item + governance.order/decision.

Propose as devrick (developer), approve/publish as steward (standards body). Direct to network.
"""
import json, urllib.request, urllib.error, os

NET = os.environ.get("NET", "http://localhost:8090")
IDN = os.environ.get("IDN", "http://localhost:8093")
ID_PAT = r"^ds:[a-z0-9-]+:\S+$"

def _post(url, body, token=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    if token: req.add_header("Authorization", "Bearer " + token)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code} {e.read().decode()[:300]}"

def login(realm, user):
    r, err = _post(f"{IDN}/realms/{realm}/login", {"username": user})
    if err: raise SystemExit(f"login {user}@{realm} failed: {err}")
    return r["access_token"]

STOKEN = login("airawat", "steward")
DTOKEN = login("airawatdeveloper", "devrick")

S  = lambda: {"type": "string"}
Sn = lambda: {"type": ["string", "null"]}

HEARING = {
    "type": "object", "x-kind": "event", "additionalProperties": False,
    "title": "justice.hearing",
    "description": "A scheduled session in which a case is heard by the court — the 'second root' "
                   "(most actions resolve against a Hearing). Carries the sitting's date, purpose, bench, "
                   "mode and outcome; adjournment produces the next hearing.",
    "required": ["schema", "id", "case_ref", "scheduled_for", "purpose", "status", "observed_at"],
    "properties": {
        "schema": {"const": "justice.hearing"},
        "id": {"type": "string", "pattern": ID_PAT},
        "case_ref": S(),                 # justice.case id
        "court_ref": Sn(),               # justice.court id
        "scheduled_for": S(),            # date-time of the sitting
        "purpose": {"enum": ["appearance", "arguments", "evidence", "framing-of-issues",
                              "judgment", "mention", "misc"]},
        "bench": Sn(),                   # presiding judge / bench ref
        "mode": {"type": ["string", "null"], "enum": ["physical", "video", "hybrid", None]},
        "status": {"enum": ["scheduled", "on-cause-list", "in-progress", "held", "adjourned", "not-held"]},
        "outcome": Sn(),                 # short outcome note (order ref carries the formal decision)
        "next_hearing_ref": Sn(),        # set when adjourned -> the hearing it created
        "observed_at": S(),              # when this hearing event was recorded/occurred
        "recorded_at": Sn(),
        "tenant": Sn(),
    },
}

print("== schema: justice.hearing@0.1.0 ==")
pr, perr = _post(f"{NET}/v1/schema-requests",
                 {"schema_id": "justice.hearing", "version": "0.1.0", "definition": HEARING,
                  "x_kind": "event", "justification": "Justice CM slice 1 — the scheduled hearing (2nd root)."},
                 token=DTOKEN)
if perr and "exists" not in perr.lower():
    print("  propose:", perr)
else:
    print("  propose:", (pr or {}).get("status", "ok"))
dr, derr = _post(f"{NET}/v1/schema-requests/decide",
                 {"schema_id": "justice.hearing", "version": "0.1.0", "decision": "approve"}, token=STOKEN)
print("  approve:", ("OK " + dr["published_schema"]) if dr else ("ERR " + str(derr)))

print("== roles ==")
for role, label in [("justice.judge", "Judge"),
                    ("justice.bench-clerk", "Bench Clerk"),
                    ("justice.scrutiny-officer", "Scrutiny Officer")]:
    r, err = _post(f"{NET}/v1/roles", {"role": role, "label": label,
                   "definition": ""}, token=STOKEN)
    print(f"  {role}:", ("OK " + r["published"]["id"]) if r else ("ERR " + str(err)))
