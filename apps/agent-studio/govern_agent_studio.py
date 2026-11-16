#!/usr/bin/env python3
"""Agent Studio — govern platform.agent_skill and platform.agent_profile.

A SKILL is a playbook the personal assistant follows when a question matches it: when to use it, its steps, the
registries it reads, the rule sets it applies, how to answer, and example questions. Draft until an administrator
makes it active; only active skills reach the assistant. An AGENT (profile) is a persona plus a set of skills plus
who it is for; the personal assistant is the default agent (every active skill the user may use).
Tenant configuration, not personal content: sensitivity "internal". The skill text goes into the assistant's prompt,
so only administrators write these (Agent Studio declares write_roles). On staging the steward files and approves.
Run where NET/IDN resolve (e.g. the app-host container)."""
import json, os, urllib.error, urllib.request

NET = os.environ.get("NET", "http://network:8090")
IDN = os.environ.get("IDN", "http://identity:8093")
ID_PAT = r"^ds:[a-z0-9-]+:\S+$"


def _post(url, body, token=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code} {e.read().decode()[:300]}"


def login(path, body):
    r, err = _post(IDN + path, body)
    if err:
        raise SystemExit("login failed: " + err.split(" ")[0])
    return r["access_token"]


Sn = lambda: {"type": ["string", "null"]}
Arr = lambda items=None: {"type": ["array", "null"], **({"items": items} if items else {})}
STATUS = {"type": "string", "enum": ["draft", "active", "retired"]}
COMMON = {"schema": None, "id": {"type": "string", "pattern": ID_PAT}, "tenant": Sn(), "owner": Sn(),
          "name": {"type": "string", "pattern": r"^[a-z][a-z0-9_]{1,47}$"}, "title": {"type": "string"},
          "status": STATUS, "audience_roles": Arr({"type": "string"}),
          "created_at": Sn(), "updated_at": Sn(), "updated_by": Sn(), "activated_by": Sn(), "activated_at": Sn(),
          "archived": {"type": ["boolean", "null"]}}

SKILL = {
    "type": "object", "x-kind": "entity", "x-subject": "agent skill", "additionalProperties": False,
    "title": "platform.agent_skill",
    "description": "A playbook the personal assistant follows when a question matches it: when to use it, the steps, "
                   "the registries it reads, the rule sets it applies, how to answer, and example questions. Only "
                   "active skills reach the assistant; the assistant still reads only what the asking user may read.",
    "required": ["schema", "id", "name", "title", "status", "when_to_use", "steps"],
    "properties": {**COMMON, "schema": {"const": "platform.agent_skill"},
        "when_to_use": {"type": "string"},
        "steps": {"type": "string"},
        "registries": Arr({"type": "object", "properties": {"schema": {"type": "string"}, "fields": Arr({"type": "string"}),
                                                            "note": Sn()}, "required": ["schema"]}),
        "rulesets": Arr({"type": "string"}),
        "answer_format": Sn(),
        "examples": Arr({"type": "string"}),
        "version": {"type": ["integer", "null"]}},
}
PROFILE = {
    "type": "object", "x-kind": "entity", "x-subject": "agent profile", "additionalProperties": False,
    "title": "platform.agent_profile",
    "description": "An agent: a persona, the skills it may use, and who it is for. The personal assistant is the "
                   "default agent, with every active skill the user may use.",
    "required": ["schema", "id", "name", "title", "status"],
    "properties": {**COMMON, "schema": {"const": "platform.agent_profile"},
        "persona": Sn(),
        "skills": Arr({"type": "string"}),
        "description": Sn()},
}

if __name__ == "__main__":
    STOKEN = login("/login", {"username": "steward"})
    for sid, d, why in (("platform.agent_skill", SKILL, "Agent Studio — playbooks the personal assistant follows."),
                        ("platform.agent_profile", PROFILE, "Agent Studio — agents: a persona plus a set of skills.")):
        print(f"== schema: {sid}@0.1.0 ==")
        pr, perr = _post(f"{NET}/v1/schema-requests", {"schema_id": sid, "version": "0.1.0", "definition": d, "x_kind": "entity",
                         "sensitivity": "internal", "justification": why}, token=STOKEN)
        print("  propose:", (pr or {}).get("status", "ok") if (not perr or "exists" in perr.lower()) else perr)
        dr, derr = _post(f"{NET}/v1/schema-requests/decide", {"schema_id": sid, "version": "0.1.0", "decision": "approve"}, token=STOKEN)
        print("  approve:", ("OK " + dr["published_schema"]) if dr else ("ERR " + str(derr)))
