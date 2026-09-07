#!/usr/bin/env python3
"""Workspace Sheets — govern doc.sheet (the spreadsheet noun). `doc` domain already governed (docs/govern.py).
Propose as devrick, approve as steward. Run where NET/IDN resolve (e.g. app-host)."""
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
Sn = lambda: {"type": ["string", "null"]}

SHEET = {
    "type": "object", "x-kind": "entity", "x-subject": "spreadsheet", "additionalProperties": False,
    "title": "doc.sheet",
    "description": "A user's spreadsheet in Workspace Sheets. `cells` maps a cell ref (e.g. \"A1\") to its raw "
                   "value — a literal or a formula string starting with '='. Owner-scoped personal content; every "
                   "save is a new bitemporal version.",
    "required": ["schema", "id", "title", "owner"],
    "properties": {
        "schema": {"const": "doc.sheet"},
        "id": {"type": "string", "pattern": ID_PAT},
        "title": {"type": "string"},
        "owner": {"type": "string"},
        "cells": {"type": ["object", "null"]},          # {"A1": "10", "B2": "=SUM(A1:A5)", …}
        "rows": {"type": ["integer", "null"]},
        "cols": {"type": ["integer", "null"]},
        "archived": {"type": ["boolean", "null"]},
        "created_at": Sn(), "updated_at": Sn(), "tenant": Sn(),
    },
}

print("== schema: doc.sheet@0.1.0 ==")
pr, perr = _post(f"{NET}/v1/schema-requests",
                 {"schema_id": "doc.sheet", "version": "0.1.0", "definition": SHEET,
                  "x_kind": "entity", "justification": "Workspace Sheets — the spreadsheet noun."}, token=DTOKEN)
print("  propose:", (pr or {}).get("status", "ok") if (not perr or "exists" in perr.lower()) else perr)
dr, derr = _post(f"{NET}/v1/schema-requests/decide",
                 {"schema_id": "doc.sheet", "version": "0.1.0", "decision": "approve"}, token=STOKEN)
print("  approve:", ("OK " + dr["published_schema"]) if dr else ("ERR " + str(derr)))
