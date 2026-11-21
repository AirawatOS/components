#!/usr/bin/env python3
"""Settings — govern platform.member_role: one of a tenant's MEMBER ROLES (ADR-0136) — who may register with the
organisation (for example a ride-hailing company's partners and customers), the profile fields asked for, the
registration steps (mobile OTP, Aadhaar check, documents), how a registration is approved (automatically, or by the
organisation's own review workflow -> a task in the staff Task Inbox) and which networks the organisation has joined
that this role may link to.

One record per role (id ds:<tenant>:member_role/<role_id>), written by a tenant administrator in Settings -> Members
(through the app-host's /bridge/member-roles, which checks the steps, the governed staff roles of the review steps and
that each network is one the organisation has joined). Tenant configuration: sensitivity "internal". Run where NET/IDN
resolve (e.g. the app-host container)."""
import json, os, urllib.error, urllib.request

NET = os.environ.get("NET", "http://network:8090")
IDN = os.environ.get("IDN", "http://identity:8093")


def _post(url, body, token=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code} {e.read().decode()[:300]}"


S = {"type": "string"}
Sn = {"type": ["string", "null"]}
FIELD = {"type": "object", "additionalProperties": False, "required": ["name", "label"],
         "properties": {"name": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,39}$"}, "label": S, "required": {"type": "boolean"},
                        "options": {"type": "array", "items": S}}}
DOC = {"type": "object", "additionalProperties": False, "required": ["type", "label"],
       "properties": {"type": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,39}$"}, "label": S, "required": {"type": "boolean"}}}
STEP = {"type": "object", "additionalProperties": False, "required": ["step", "role"],
        "properties": {"step": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,39}$"}, "role": S, "task": Sn}}
ROLE = {
    "type": "object", "x-kind": "entity", "x-subject": "Member role", "additionalProperties": False,
    "title": "platform.member_role",
    "description": "One of an organisation's member roles: who may register with it, what registration asks for, how it "
                   "is approved, and which joined networks the role may link to (ADR-0136).",
    "required": ["schema", "id", "tenant", "role_id", "label"],
    "properties": {
        "schema": {"const": "platform.member_role"}, "id": {"type": "string", "pattern": r"^ds:[a-z0-9-]+:\S+$"},
        "tenant": S, "role_id": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,39}$"}, "label": S, "summary": Sn,
        "order": {"type": "integer"}, "enabled": {"type": "boolean"},
        "fields": {"type": "array", "items": FIELD},
        "registration": {"type": "object", "additionalProperties": False, "properties": {
            "steps": {"type": "array", "items": {"enum": ["mobile_otp", "aadhaar_ekyc", "documents"]}},
            "documents": {"type": "array", "items": DOC},
            "approval": {"type": "object", "additionalProperties": False, "required": ["mode"], "properties": {
                "mode": {"enum": ["auto", "review"]}, "title": Sn, "workflow": {"type": "array", "items": STEP}}}}},
        "networks": {"type": "array", "items": S},
        "updated_by": Sn, "updated_at": Sn},
}

if __name__ == "__main__":
    r, err = _post(IDN + "/login", {"username": "steward"})
    if err:
        raise SystemExit("login failed: " + err.split(" ")[0])
    tok = r["access_token"]
    print("== schema: platform.member_role@0.1.0 ==")
    pr, perr = _post(f"{NET}/v1/schema-requests", {"schema_id": "platform.member_role", "version": "0.1.0", "definition": ROLE,
                     "x_kind": "entity", "sensitivity": "internal",
                     "justification": "Settings -> Members: an organisation's member roles and how they register (ADR-0136)."}, token=tok)
    print("  propose:", (pr or {}).get("status", "ok") if (not perr or "exists" in perr.lower()) else perr)
    dr, derr = _post(f"{NET}/v1/schema-requests/decide", {"schema_id": "platform.member_role", "version": "0.1.0", "decision": "approve"}, token=tok)
    print("  approve:", ("OK " + dr["published_schema"]) if dr else ("ERR " + str(derr)))
