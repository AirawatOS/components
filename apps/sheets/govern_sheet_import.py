#!/usr/bin/env python3
"""Workspace Sheets — govern doc.sheet_import: one import of a scanned paper register into a sheet.

Sheets creates it (status "pending", owner = the signed-in user, stamped by the host); engine/register-ocr reads the
scan, fills `cells` (the sheet's values and formulas) and `cell_info` (per cell: state, what was read, confidence, the
suggestion and the rule behind it, and where on which page it came from), plus `pages` (page images for review), and
sets status "done". Sheets then creates the sheet, records `sheet_ref`, and keeps `cell_info` up to date as the user
reviews. Owner-scoped personal content; every save is a new bitemporal version.
Sensitivity "sensitive" (registers hold pay and PF data). On staging the steward files and approves (a devrick filing
from the development tenant is only auto-approved into that tenant's quarantined vocabulary). Run where NET/IDN resolve (e.g. app-host)."""
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


STOKEN = login("/login", {"username": "steward"})
Sn = lambda: {"type": ["string", "null"]}

IMPORT = {
    "type": "object", "x-kind": "entity", "x-subject": "sheet import", "additionalProperties": False,
    "title": "doc.sheet_import",
    "description": "One import of a scanned paper register (e.g. a P.F.-1 Provident Fund Ledger) into a Workspace "
                   "sheet: the scan, the register type, the values read, and per cell how sure the reading is and "
                   "where on the page it came from, so the user can review flagged cells against the original. "
                   "Owner-scoped personal content.",
    "required": ["schema", "id", "owner", "form", "status"],
    "properties": {
        "schema": {"const": "doc.sheet_import"},
        "id": {"type": "string", "pattern": ID_PAT},
        "owner": {"type": "string"},
        "tenant": Sn(),
        "title": Sn(),
        "form": {"type": "string"},                       # register type, e.g. "pf1"
        "status": {"type": "string"},                     # pending | running | done | failed
        "source_files": {"type": ["array", "null"]},      # [{file_id, name, type}]
        "pages": {"type": ["array", "null"]},             # [{page, file_id (image), width, height, source}]
        "cells": {"type": ["object", "null"]},            # ref → raw value / formula, as for doc.sheet
        "cell_info": {"type": ["object", "null"]},        # ref → {state, read, conf, suggest, rule, how, page, box}
        "summary": {"type": ["object", "null"]},          # counts: blocks, rules passed, corrections, to check …
        "sheet_ref": Sn(),                                # the doc.sheet this import created / updated
        "engine": Sn(),
        "error": Sn(),
        "requested_at": Sn(), "started_at": Sn(), "finished_at": Sn(), "created_at": Sn(), "updated_at": Sn(),
        "archived": {"type": ["boolean", "null"]},
    },
}

print("== schema: doc.sheet_import@0.1.0 ==")
pr, perr = _post(f"{NET}/v1/schema-requests", {"schema_id": "doc.sheet_import", "version": "0.1.0", "definition": IMPORT,
                 "x_kind": "entity", "sensitivity": "sensitive", "justification": "Workspace Sheets — import a scanned paper register into a sheet, with review."}, token=STOKEN)
print("  propose:", (pr or {}).get("status", "ok") if (not perr or "exists" in perr.lower()) else perr)
dr, derr = _post(f"{NET}/v1/schema-requests/decide", {"schema_id": "doc.sheet_import", "version": "0.1.0", "decision": "approve"}, token=STOKEN)
print("  approve:", ("OK " + dr["published_schema"]) if dr else ("ERR " + str(derr)))
