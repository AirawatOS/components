#!/usr/bin/env python3
"""Bump justice.disposal 0.2.0 → 0.3.0: add `order_text` (the judge's pronounced order / judgment narrative,
nullable) so Court Console can record the actual order — including by voice dictation. Resolves the current
def, adds the field, re-proposes (devrick) + approves (steward). Run where NET/IDN resolve (inside app-host).
Usage: NET=http://airawat-network:8090 IDN=http://identity:8093 python3 govern_disposal_order.py"""
import json, urllib.request, urllib.error, os
NET = os.environ.get("NET", "http://airawat-network:8090"); IDN = os.environ.get("IDN", "http://identity:8093")
def _post(url, body, token=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    if token: req.add_header("Authorization", "Bearer " + token)
    try: return json.loads(urllib.request.urlopen(req, timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:300]}"
def login(realm, user):
    r, err = _post(f"{IDN}/realms/{realm}/login", {"username": user})
    if err: raise SystemExit(f"login {user}@{realm} failed: {err}")
    return r["access_token"]

cur, err = _post(f"{NET}/v1/schemas/resolve", {"schema_id": "justice.disposal", "version": "0.2.0"})
if err: raise SystemExit("resolve 0.2.0 failed: " + err)
defn = (cur.get("schema") or cur).get("definition") or {}
if "order_text" not in defn.get("properties", {}):
    defn.setdefault("properties", {})["order_text"] = {"type": ["string", "null"]}   # the pronounced order / judgment text
xk = defn.get("x-kind") or "event"

STOKEN = login("airawat", "steward"); DTOKEN = login("airawatdeveloper", "devrick")
print("== schema: justice.disposal@0.3.0 (add order_text) ==")
pr, perr = _post(f"{NET}/v1/schema-requests", {"schema_id": "justice.disposal", "version": "0.3.0", "definition": defn,
                 "x_kind": xk, "justification": "Court Console — record the judge's pronounced order/judgment text (voice-dictatable)."}, token=DTOKEN)
print("  propose:", (pr or {}).get("status", "ok") if (not perr or "exists" in perr.lower()) else perr)
dr, derr = _post(f"{NET}/v1/schema-requests/decide", {"schema_id": "justice.disposal", "version": "0.3.0", "decision": "approve"}, token=STOKEN)
print("  approve:", ("OK " + dr["published_schema"]) if dr else ("ERR " + str(derr)))
