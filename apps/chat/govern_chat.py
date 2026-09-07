#!/usr/bin/env python3
"""Workspace Chat — govern chat.channel + chat.message. The `chat` domain is already governed (see docs/govern.py).
Propose as devrick (developer), approve as steward (standards body). Run where NET/IDN resolve (e.g. app-host)."""
import json, urllib.request, urllib.error, os

NET = os.environ.get("NET", "http://airawat-network:8090")
IDN = os.environ.get("IDN", "http://identity:8093")
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

CHANNEL = {
    "type": "object", "x-kind": "entity", "x-subject": "chat-channel", "additionalProperties": False,
    "title": "chat.channel",
    "description": "A conversation in Workspace Chat. Members are PRINCIPALS — user:// (a person), agent:// (an AI "
                   "assistant/expert) or participant:// (someone in another tenant). A member that is an agent "
                   "answers automatically, so human↔human, human↔AI and mixed groups are all the same channel.",
    "required": ["schema", "id", "title", "members", "created_by"],
    "properties": {
        "schema": {"const": "chat.channel"},
        "id": {"type": "string", "pattern": ID_PAT},
        "title": S(),
        "kind": {"type": ["string", "null"], "enum": ["dm", "group", "ai", None]},
        "members": {"type": "array", "items": {"type": "string"}},   # principals (user://, agent://, participant://)
        "created_by": S(),
        "last_message_at": Sn(),
        "created_at": Sn(),
        "updated_at": Sn(),
        "tenant": Sn(),
    },
}

MESSAGE = {
    "type": "object", "x-kind": "event", "x-subject": "chat-message", "additionalProperties": False,
    "title": "chat.message",
    "description": "One message in a chat.channel. sender is a principal (a person or an AI agent). Body is plain "
                   "text; an AI member's reply is just another message whose sender is the agent.",
    "required": ["schema", "id", "channel_ref", "sender", "body", "observed_at"],
    "properties": {
        "schema": {"const": "chat.message"},
        "id": {"type": "string", "pattern": ID_PAT},
        "channel_ref": S(),
        "sender": S(),                                   # user://… or agent://…
        "body": S(),
        "kind": {"type": ["string", "null"], "enum": ["text", "ai", "system", None]},
        "observed_at": S(),
        "tenant": Sn(),
    },
}

for sid, defn, xk in [("chat.channel", CHANNEL, "entity"), ("chat.message", MESSAGE, "event")]:
    print(f"== schema: {sid}@0.1.0 ==")
    pr, perr = _post(f"{NET}/v1/schema-requests",
                     {"schema_id": sid, "version": "0.1.0", "definition": defn,
                      "x_kind": xk, "justification": "Workspace Chat — unified human + AI conversation."}, token=DTOKEN)
    print("  propose:", (pr or {}).get("status", "ok") if (not perr or "exists" in perr.lower()) else perr)
    dr, derr = _post(f"{NET}/v1/schema-requests/decide",
                     {"schema_id": sid, "version": "0.1.0", "decision": "approve"}, token=STOKEN)
    print("  approve:", ("OK " + dr["published_schema"]) if dr else ("ERR " + str(derr)))
