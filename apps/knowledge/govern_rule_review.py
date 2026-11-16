#!/usr/bin/env python3
"""Knowledge rule review + Agent Studio knowledge — schema upgrades, each built from the LIVE definition (only fields
are added; nothing existing changes).

governance.rule 0.3.0 (from 0.2.0): a reviewer can REJECT a proposed rule (status "rejected") with a reason, or EDIT
it (it goes back to "proposed" and needs approval again). Adds reviewed_by, reviewed_at, review_note, edited_by,
edited_at. `value` stays a LIST: [x] for a threshold, [min, max] for "between".

platform.agent_skill 0.2.0 (from 0.1.0): `knowledge` — the Knowledge documents and topics a skill draws on
([{doc, title, topic, note}]); the assistant then calls find_rules / search_knowledge for them.
Run where NET/IDN resolve (e.g. the app-host container)."""
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


def current(sid):
    r, err = _post(NET + "/v1/schemas/resolve", {"schema_id": sid})
    if err:
        raise SystemExit(f"cannot read {sid}: {err}")
    s = r.get("schema") or r
    return s.get("version"), s.get("sensitivity") or "internal", json.loads(json.dumps(s.get("definition") or {}))


Sn = lambda: {"type": ["string", "null"]}
UPGRADES = [
    ("governance.rule", "0.2.0", "0.3.0",
     {"reviewed_by": Sn(), "reviewed_at": Sn(), "review_note": Sn(), "edited_by": Sn(), "edited_at": Sn()},
     "Knowledge — reviewers can reject a proposed rule with a reason, or edit it (back to proposed)."),
    ("platform.agent_skill", "0.1.0", "0.2.0",
     {"knowledge": {"type": ["array", "null"], "items": {"type": "object", "required": ["doc"], "properties": {
         "doc": {"type": "string"}, "title": Sn(), "topic": Sn(), "note": Sn()}}}},
     "Agent Studio — a skill names the Knowledge documents and topics it draws on."),
]

if __name__ == "__main__":
    r, err = _post(IDN + "/login", {"username": "steward"})
    if err:
        raise SystemExit("login failed: " + err.split(" ")[0])
    tok = r["access_token"]
    for sid, want_from, ver, add, why in UPGRADES:
        have, sens, d = current(sid)
        if have == ver:
            print(f"== {sid}: already {ver}"); continue
        if have != want_from:
            print(f"== {sid}: live version is {have}, expected {want_from} — skipped"); continue
        d.setdefault("properties", {}).update(add)
        for k in ("$id",):
            d.pop(k, None)
        print(f"== schema: {sid} {have} → {ver} (+ {', '.join(add)}) ==")
        pr, perr = _post(f"{NET}/v1/schema-requests", {"schema_id": sid, "version": ver, "definition": d,
                         "x_kind": d.get("x-kind"), "sensitivity": sens, "justification": why}, token=tok)
        print("  propose:", (pr or {}).get("status", "ok") if not perr else perr)
        dr, derr = _post(f"{NET}/v1/schema-requests/decide", {"schema_id": sid, "version": ver, "decision": "approve"}, token=tok)
        print("  approve:", ("OK " + dr["published_schema"]) if dr else ("ERR " + str(derr)))
