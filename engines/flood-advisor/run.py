#!/usr/bin/env python3
"""flood-advisor engine — the ADVISE stage as a governed, event-triggered component (ADR-0063/0050).

The automation dispatcher fires this when a signal.alert is written, passing SUBJECT_REF (the alert id) into
the env via the run verb. This engine reads that specific alert + the knowledge base and emits cited
governance.recommendation[] + opens the platform.case + decision task + inbox item. All I/O goes through the
BROKER under the run-token minted at install — the confined path, not a direct registry write.

Env: SUBJECT_REF (the signal.alert id that triggered this) · TENANT/WRITE_TENANT · RUN_TOKEN
"""
import json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
SUBJECT_REF = os.environ.get("SUBJECT_REF", "")
TENANT = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT", "kmc")
DECIDER = os.environ.get("DECIDER", f"user://{TENANT}/kanpur-admin")
LEVEL_RANK = {"low": 0, "moderate": 1, "high": 2, "severe": 3}
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def read(scope, oid):
    return _call("/v1/read", {"scope": scope, "id": oid}).get("object")


def query(scope):
    return [r.get("object", r) for r in _call("/v1/query", {"scope": scope}).get("results", [])]


def write(scope, oid, obj):
    obj = {k: v for k, v in obj.items() if v is not None}   # omit unset optional fields (house rule R4)
    _call("/v1/write", {"scope": scope, "id": oid, "object": obj, "valid_time": {"from": NOW}, "asserted_at": NOW})


def main():
    if not SUBJECT_REF:
        print("[flood-advisor] no SUBJECT_REF — nothing to advise on"); return 0
    alert = read("signal.alert", SUBJECT_REF)
    if not alert or alert.get("schema") != "signal.alert":
        print(f"[flood-advisor] SUBJECT_REF {SUBJECT_REF} is not a signal.alert"); return 1

    sev = alert.get("severity", "high"); top = alert.get("top", [])
    maxscore = alert.get("maxscore", ""); count = alert.get("count", 0)
    run = alert["id"].rsplit("/", 1)[-1].replace("flood-", "")
    case_id = f"ds:{TENANT}:case/flood-{run}"; task_id = f"ds:{TENANT}:task/flood-{run}"

    guides = [g for g in query("knowledge.guideline") if g.get("applies_to") == "flood"
              and LEVEL_RANK.get(g.get("trigger_level", "high"), 2) <= LEVEL_RANK.get(sev, 2)]
    guides.sort(key=lambda g: -g.get("confidence", 0.5))
    rec_ids = []
    for i, g in enumerate(guides):
        rid = f"ds:{TENANT}:rec/flood-{run}-{i}"
        write("governance.recommendation", rid, {"schema": "governance.recommendation", "id": rid,
              "case": case_id, "for_task": task_id, "title": g["title"],
              "summary": g.get("proposed_action", ""), "proposed_action": g.get("proposed_action", ""),
              "rationale": f"{count} cells breach risk threshold (max {maxscore}); {g.get('citation','')} prescribes this at {g.get('trigger_level')}+ risk.",
              "evidence": [g["id"]] + top, "citation": g.get("citation", ""), "confidence": g.get("confidence", 0.5),
              "method": "rule", "produced_by": "component://flood-advisor@0.1.0", "score": g.get("confidence", 0.5),
              "sop_workflow": g.get("sop_workflow"), "created_at": NOW, "tenant": TENANT})
        rec_ids.append(rid)

    write("platform.case", case_id, {"schema": "platform.case", "id": case_id,
          "title": f"Flood response — {count} low-lying cells", "service": f"ds:{TENANT}:solution/flood-response",
          "workflow": f"ds:{TENANT}:workflow/flood", "status": "active", "current_step": "decide",
          "opened_by": "component://flood-advisor@0.1.0", "opened_at": NOW, "updated_at": NOW,
          "subjects": top + [alert["id"]], "tenant": TENANT,
          "history": [{"at": NOW, "event": "opened", "actor": "component://flood-advisor@0.1.0", "ref": alert["id"],
                       "summary": f"Opened from flood alert (severity {sev}) — event-triggered via SUBJECT_REF"}]})
    write("platform.task", task_id, {"schema": "platform.task", "id": task_id, "case": case_id, "verb": "decide",
          "title": f"Decide flood response — {count} low-lying cells", "subjects": top, "assignee": DECIDER,
          "workflow_step": "decide", "decision_kind": "select", "options": rec_ids,
          "actions": ["approve", "reject"], "status": "open", "created_at": NOW, "tenant": TENANT})
    iid = f"ds:{TENANT}:platform/inbox_item/flood-{run}"
    write("platform.inbox_item", iid, {"schema": "platform.inbox_item", "id": iid, "actor": DECIDER,
          "title": f"Decide flood response — {count} low-lying cells",
          "body": f"Alert {sev}. {len(rec_ids)} guideline-based options await your decision.",
          "ref": task_id, "kind": "task", "actions": ["approve", "reject"], "status": "unread",
          "source": "flood-advisor", "created_at": NOW})

    print(f"[flood-advisor] advised on {alert['id']}: {len(rec_ids)} recs + case {case_id} + decision task")
    print(f"RESULT: {json.dumps({'case': case_id, 'task': task_id, 'recommendations': rec_ids})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
