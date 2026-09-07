#!/usr/bin/env python3
"""Workspace suite — govern the base-OS office vocabulary.

Adds the sector DOMAINS `doc`, `mail`, `chat` (the namespaces the productivity suite writes in) and governs
`doc.document@0.1.0` (the text-editor's noun). Propose as devrick (developer), approve as steward (standards
body). Run inside a container that reaches the network + identity by service DNS (e.g. airawat-app-host)."""
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

print("== domains ==")
for dom, label in [("doc", "Documents & office content"), ("mail", "Internal mail"), ("chat", "Chat & messaging")]:
    r, err = _post(f"{NET}/v1/domains", {"domain": dom, "label": label}, token=STOKEN)
    print(f"  {dom}:", (r or {}).get("published", {}).get("domain") if r else ("(exists?) " + str(err)))

DOCUMENT = {
    # x-subject distinguishes this editable OFFICE document from knowledge.document (an ingested/cited source
    # doc) — same word, different noun, so ADR-0060 §3a's (subject,layer,kind) collision rule needs the facet.
    "type": "object", "x-kind": "entity", "x-subject": "office-document", "additionalProperties": False, "title": "doc.document",
    "description": "A user's text document in the Workspace Docs editor — the persistent thing with identity that "
                   "the person edits. Body is a portable HTML fragment; large media/attachments live in the "
                   "filestore, referenced by file_id. Owner-scoped personal content; every save is a new "
                   "bitemporal version (free history).",
    "required": ["schema", "id", "title", "owner"],
    "properties": {
        "schema": {"const": "doc.document"},
        "id": {"type": "string", "pattern": ID_PAT},
        "title": S(),
        "body": {"type": ["string", "null"]},                 # HTML fragment
        "format": {"type": ["string", "null"], "enum": ["html", "markdown", "plain", None]},
        "owner": S(),                                          # user://<tenant>/<uid> — the author (host-forced)
        "folder": Sn(),
        "archived": {"type": ["boolean", "null"]},
        "attachments": {"type": ["array", "null"], "items": {"type": "string"}},   # filestore file_ids
        "created_at": Sn(),
        "updated_at": Sn(),
        "tenant": Sn(),
    },
}

print("== schema: doc.document@0.1.0 ==")
pr, perr = _post(f"{NET}/v1/schema-requests",
                 {"schema_id": "doc.document", "version": "0.1.0", "definition": DOCUMENT,
                  "x_kind": "entity", "justification": "Workspace suite — the Docs text editor's noun."},
                 token=DTOKEN)
if perr and "exists" not in perr.lower():
    print("  propose:", perr)
else:
    print("  propose:", (pr or {}).get("status", "ok"))
dr, derr = _post(f"{NET}/v1/schema-requests/decide",
                 {"schema_id": "doc.document", "version": "0.1.0", "decision": "approve"}, token=STOKEN)
print("  approve:", ("OK " + dr["published_schema"]) if dr else ("ERR " + str(derr)))
