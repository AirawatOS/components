#!/usr/bin/env python3
"""File Explorer — govern the `platform.file` noun (the personal-file entry the Files app writes).

The `platform` domain already exists (platform.case/task/inbox_item/...), so we only govern the schema.
A record is one node in the user's virtual file tree: a `file` (points at a content-addressed filestore blob
by file_id) or a `folder` (an empty container so the tree persists before any file lands). Owner-scoped
personal content — the app is `personal: true`, so the host forces owner==sub on every read/write.

Propose as devrick (developer), approve as steward (standards body). Run inside a container that reaches the
network + identity by service DNS (e.g. airawat-app-host).
    NET=http://airawat-network:8090 IDN=http://identity:8093 python3 govern.py
"""
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

# The `platform` domain already exists on a seeded network (platform.case/task/inbox_item/...); ensure it so this
# script also works on a bare network. Idempotent — an "exists" is expected and fine.
dr, derr = _post(f"{NET}/v1/domains", {"domain": "platform", "label": "Platform runtime nouns"}, token=STOKEN)
print("== domain platform ==", (dr or {}).get("published", {}).get("domain") if dr else ("(exists?) " + str(derr)))

FILE = {
    "type": "object", "x-kind": "entity", "x-subject": "user-file", "additionalProperties": False,
    "title": "platform.file",
    "description": "One node in a user's personal file tree in the Files app: a `file` (metadata pointing at a "
                   "content-addressed filestore blob by file_id) or a `folder` (an empty container so the tree "
                   "persists before a file lands). Bytes live in the filestore (immutable, content-addressed); "
                   "this record holds the name, parent folder and pointer. Owner-scoped personal content.",
    "required": ["schema", "id", "name", "owner"],
    "properties": {
        "schema": {"const": "platform.file"},
        "id": {"type": "string", "pattern": ID_PAT},
        "kind": {"type": ["string", "null"], "enum": ["file", "folder", None]},   # default treated as "file"
        "name": S(),                                          # leaf name shown in the tree
        "folder": Sn(),                                       # parent path, e.g. "/" or "/exports" (default "/")
        "file_id": Sn(),                                      # filestore content hash "sha2-256:…" (files only)
        "content_type": Sn(),
        "size_bytes": {"type": ["integer", "null"]},
        "owner": S(),                                         # user://<tenant>/<uid> — host-forced
        "starred": {"type": ["boolean", "null"]},
        "archived": {"type": ["boolean", "null"]},            # soft-delete (blob is immutable; the pointer hides)
        "created_at": Sn(),
        "updated_at": Sn(),
        "tenant": Sn(),
    },
}

print("== schema: platform.file@0.1.0 ==")
pr, perr = _post(f"{NET}/v1/schema-requests",
                 {"schema_id": "platform.file", "version": "0.1.0", "definition": FILE,
                  "x_kind": "entity", "justification": "Files app — the personal-file entry (Basic Apps suite)."},
                 token=DTOKEN)
if perr and "exists" not in perr.lower():
    print("  propose:", perr)
else:
    print("  propose:", (pr or {}).get("status", "ok"))
dr, derr = _post(f"{NET}/v1/schema-requests/decide",
                 {"schema_id": "platform.file", "version": "0.1.0", "decision": "approve"}, token=STOKEN)
print("  approve:", ("OK " + dr["published_schema"]) if dr else ("ERR " + str(derr)))
