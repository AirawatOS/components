#!/usr/bin/env python3
"""Assistant — govern platform.agent_chat: one saved conversation with the personal assistant.

Listed in the Assistant's left pane (like documents in Docs). Owned by its creator (x-ownership "user": the kernel
stamps the owner from the writer's identity, and only the owner may change it); the Assistant is a personal app, so its
reads are owner-scoped too. Sensitivity "sensitive": a chat can hold whatever the assistant read for the user (tasks,
mail, documents). Each save is a new bitemporal version. Run where NET/IDN resolve (e.g. the app-host container)."""
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


Sn = lambda: {"type": ["string", "null"]}
MSG = {"type": "object", "required": ["role", "content"], "properties": {
    "role": {"type": "string", "enum": ["user", "assistant", "note"]}, "content": {"type": "string"},
    "at": Sn(), "steps": {"type": ["array", "null"], "items": {"type": "string"}},
    "link": Sn(), "error": {"type": ["boolean", "null"]}, "proposal": {"type": ["object", "null"]}}}
CHAT = {
    "type": "object", "x-kind": "record", "x-subject": "assistant conversation", "x-ownership": "user",
    "additionalProperties": False, "title": "platform.agent_chat",
    "description": "One saved conversation with the personal assistant: its title, the agent it was with, and its "
                   "messages (with the tools each answer used). Owned by its creator.",
    "required": ["schema", "id", "title", "messages"],
    "properties": {"schema": {"const": "platform.agent_chat"}, "id": {"type": "string", "pattern": r"^ds:[a-z0-9-]+:\S+$"},
                   "tenant": Sn(), "owner": Sn(), "title": {"type": "string"}, "agent": Sn(), "agent_title": Sn(),
                   "messages": {"type": "array", "items": MSG}, "created_at": Sn(), "updated_at": Sn(),
                   "archived": {"type": ["boolean", "null"]}},
}

if __name__ == "__main__":
    r, err = _post(IDN + "/login", {"username": "steward"})
    if err:
        raise SystemExit("login failed: " + err.split(" ")[0])
    tok = r["access_token"]
    print("== schema: platform.agent_chat@0.1.0 ==")
    pr, perr = _post(f"{NET}/v1/schema-requests", {"schema_id": "platform.agent_chat", "version": "0.1.0", "definition": CHAT,
                     "x_kind": "record", "sensitivity": "sensitive",
                     "justification": "Assistant — saved conversations in the left pane."}, token=tok)
    print("  propose:", (pr or {}).get("status", "ok") if (not perr or "exists" in perr.lower()) else perr)
    dr, derr = _post(f"{NET}/v1/schema-requests/decide", {"schema_id": "platform.agent_chat", "version": "0.1.0", "decision": "approve"}, token=tok)
    print("  approve:", ("OK " + dr["published_schema"]) if dr else ("ERR " + str(derr)))
