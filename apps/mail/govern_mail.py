#!/usr/bin/env python3
"""Workspace Mail — govern mail.message. `mail` domain already governed. devrick proposes, steward approves."""
import json, urllib.request, urllib.error, os
NET = os.environ.get("NET", "http://airawat-network:8090"); IDN = os.environ.get("IDN", "http://identity:8093")
ID_PAT = r"^ds:[a-z0-9-]+:\S+$"
def _post(url, body, token=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    if token: req.add_header("Authorization", "Bearer " + token)
    try: return json.loads(urllib.request.urlopen(req, timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:300]}"
def login(realm, user):
    r, err = _post(f"{IDN}/realms/{realm}/login", {"username": user})
    if err: raise SystemExit(f"login {user}@{realm} failed: {err}")
    return r["access_token"]
STOKEN = login("airawat", "steward"); DTOKEN = login("airawatdeveloper", "devrick")
Sn = lambda: {"type": ["string", "null"]}
MESSAGE = {
    "type": "object", "x-kind": "event", "x-subject": "mail-message", "additionalProperties": False, "title": "mail.message",
    "description": "An internal/federated mail message. Visible to its sender and recipients (principals — "
                   "user:// or, across tenants, participant://). Threaded via thread_ref; read_by tracks who has "
                   "opened it. Attachments (later) live in the filestore by file_id.",
    "required": ["schema", "id", "sender", "recipients", "subject", "observed_at"],
    "properties": {
        "schema": {"const": "mail.message"}, "id": {"type": "string", "pattern": ID_PAT},
        "sender": {"type": "string"}, "recipients": {"type": "array", "items": {"type": "string"}},
        "subject": {"type": "string"}, "body": Sn(), "thread_ref": Sn(),
        "read_by": {"type": ["array", "null"], "items": {"type": "string"}},
        "attachments": {"type": ["array", "null"], "items": {"type": "string"}},
        "observed_at": {"type": "string"}, "tenant": Sn(),
    },
}
print("== schema: mail.message@0.1.0 ==")
pr, perr = _post(f"{NET}/v1/schema-requests", {"schema_id": "mail.message", "version": "0.1.0", "definition": MESSAGE,
                 "x_kind": "event", "justification": "Workspace Mail — internal/federated message."}, token=DTOKEN)
print("  propose:", (pr or {}).get("status", "ok") if (not perr or "exists" in perr.lower()) else perr)
dr, derr = _post(f"{NET}/v1/schema-requests/decide", {"schema_id": "mail.message", "version": "0.1.0", "decision": "approve"}, token=STOKEN)
print("  approve:", ("OK " + dr["published_schema"]) if dr else ("ERR " + str(derr)))
