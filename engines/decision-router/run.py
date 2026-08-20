#!/usr/bin/env python3
"""decision-router engine: actionable governance.recommendation[] → platform.case + task + inbox_item.

The ADMIT-ROUTING step of the Decision spine (ADR-0029: AI never decides). Confined — only I/O is the
broker. For each actionable recommendation it opens the decision loop for a human AUTHORITY: a
platform.case, a platform.task (verb=decide, options=[the recommendation]), and a platform.inbox_item
addressed to the deciding ROLE so it lands in that person's Inbox. The authority admits/rejects it
downstream (decision-service → signed governance.decision, whose evidence_manifest is the receipt).
Idempotent — case/task/inbox ids derive from the recommendation id, so re-runs don't duplicate.
"""
import json, os, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
WRITE_TENANT = os.environ.get("WRITE_TENANT", "kmc")
DECIDER = os.environ.get("DECIDER", "role://kmc/urban.disaster-officer")   # who must decide (a role or user)
CID = "ds:airawat:engine/decision-router"


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def main():
    recs = [r.get("object") or r for r in _call("/v1/query", {"scope": "governance.recommendation", "limit": 1000}).get("results", [])]
    # actionable = has a real proposed action (skip "No action required." advisories)
    recs = [r for r in recs if r.get("proposed_action") and not r["proposed_action"].lower().startswith("no action")]
    if not recs:
        print("no actionable governance.recommendation to route."); return 0

    # ALREADY-ROUTED guard: skip any recommendation that already has a decision task (don't re-open a
    # case that may already be decided/closed). Idempotent — routing runs are safe to repeat.
    existing_tasks = {t.get("id") for t in (r.get("object") or r
                      for r in _call("/v1/query", {"scope": "platform.task", "limit": 5000}).get("results", []))}

    routed = skipped = 0
    for rec in recs:
        base = rec["id"].rsplit("/", 1)[-1]
        case_id = f"ds:{WRITE_TENANT}:case/{base}"
        task_id = f"ds:{WRITE_TENANT}:task/{base}"
        inbox_id = f"ds:{WRITE_TENANT}:inbox_item/{base}"
        if task_id in existing_tasks:
            skipped += 1; continue                        # already routed — leave it alone
        now = rec.get("created_at") or ""
        ev = rec.get("evidence") or {}
        subjects = [rec["id"]] + ([ev.get("aoi")] if isinstance(ev, dict) and ev.get("aoi") else [])

        _call("/v1/write", {"scope": "platform.case", "id": case_id, "object": {
            "schema": "platform.case", "id": case_id, "title": rec.get("title", "Decision"),
            "status": "open", "current_step": "decide", "subjects": subjects,
            "opened_by": f"component://{CID.split(':',2)[-1]}", "opened_at": now, "updated_at": now,
            "tenant": WRITE_TENANT,
            "history": [{"at": now, "event": "opened", "actor": f"component://{CID.split(':',2)[-1]}",
                         "ref": rec["id"], "summary": "Opened from a rules recommendation — awaiting authority decision"}]},
            "valid_time": {"from": now}, "asserted_at": now})

        _call("/v1/write", {"scope": "platform.task", "id": task_id, "object": {
            "schema": "platform.task", "id": task_id, "case": case_id, "verb": "decide",
            "title": f"Decide: {rec.get('title', '')}", "subjects": subjects, "assignee": DECIDER,
            "workflow_step": "decide", "decision_kind": "approve", "options": [rec["id"]],
            "actions": ["approve", "reject"], "status": "open", "created_at": now, "tenant": WRITE_TENANT},
            "valid_time": {"from": now}, "asserted_at": now})

        _call("/v1/write", {"scope": "platform.inbox_item", "id": inbox_id, "object": {
            "schema": "platform.inbox_item", "id": inbox_id, "actor": DECIDER, "kind": "decision",
            "title": rec.get("title", "Decision required"), "body": rec.get("summary", ""),
            "ref": task_id, "handler": "decide", "actions": ["approve", "reject"],
            "source": f"component://{CID.split(':',2)[-1]}", "status": "unread",
            "created_at": now, "tenant": WRITE_TENANT},
            "valid_time": {"from": now}, "asserted_at": now})

        routed += 1
        print(f"  routed → case {case_id.rsplit('/',1)[-1]} · task (decide) · inbox → {DECIDER}")

    print(f"routed {routed} new recommendation(s) into the decision loop ({skipped} already routed, skipped); "
          f"inbox items addressed to {DECIDER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
