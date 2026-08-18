#!/usr/bin/env python3
"""Flood ADVISOR service — the domain component the automation fires when a flood alert is written.
It is the ADVISE stage as an EVENT HANDLER: given a signal.alert (the event subject), it reads the
KNOWLEDGE BASE (knowledge.guideline), emits cited governance.recommendation[] and opens the
platform.case + decision task + inbox item the decision loop consumes.

  POST /advise  {event, subject}   (subject = the signal.alert)   ← fired by the automation service
  GET  /healthz

Generic fabric (event → subscription → action) fires this; the advisor itself is DOMAIN (flood) and
rides on top. Writes to the registry directly (reference); production = broker + provenance.
"""
import argparse, json, os, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KERNEL = os.environ.get("AIRAWAT_API", "http://localhost:8080")
NOTIFY = os.environ.get("NOTIFY_URL", "http://localhost:8094")
DECIDER = os.environ.get("DECIDER", "user://kmc/kanpur-admin")
LEVEL_RANK = {"low": 0, "moderate": 1, "high": 2, "severe": 3}


def _find(schema):
    r = urllib.request.Request(KERNEL + "/v1/registry/resolve",
        data=json.dumps({"filter": {"schema": schema}, "limit": 500}).encode(), headers={"Content-Type": "application/json"})
    return [x["object"] for x in json.loads(urllib.request.urlopen(r, timeout=10).read()).get("results", [])]

def _put(obj):
    urllib.request.urlopen(urllib.request.Request(KERNEL + "/v1/registry/objects",
        data=json.dumps({"id": obj["id"], "object": obj}).encode(), headers={"Content-Type": "application/json"}), timeout=8).read()

def _notify(p):
    try:
        urllib.request.urlopen(urllib.request.Request(NOTIFY + "/notify",
            data=json.dumps(p).encode(), headers={"Content-Type": "application/json"}), timeout=8).read()
    except Exception as e:
        print("  (notify failed:", e, ")")

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def advise(alert):
    tenant = alert.get("tenant", "kmc")
    sev = alert.get("severity", "high")
    top = alert.get("top", [])
    maxscore = alert.get("maxscore", "")
    count = alert.get("count", 0)
    run = alert["id"].rsplit("/", 1)[-1].replace("flood-", "")
    case_id = f"ds:{tenant}:case/flood-{run}"
    task_id = f"ds:{tenant}:task/flood-{run}"

    # read the KNOWLEDGE BASE and pick guidelines whose trigger level the alert has reached
    guides = [g for g in _find("knowledge.guideline") if g.get("applies_to") == "flood"
              and LEVEL_RANK.get(g.get("trigger_level", "high"), 2) <= LEVEL_RANK.get(sev, 2)]
    guides.sort(key=lambda g: -g.get("confidence", 0.5))
    rec_ids = []
    for i, g in enumerate(guides):
        rid = f"ds:{tenant}:rec/flood-{run}-{i}"
        _put({"schema": "governance.recommendation", "id": rid, "case": case_id, "for_task": task_id,
              "title": g["title"], "summary": g.get("proposed_action", ""), "proposed_action": g.get("proposed_action", ""),
              "rationale": f"{count} cells breach risk threshold (max {maxscore}); {g.get('citation','')} prescribes this at {g.get('trigger_level')}+ risk.",
              "evidence": [g["id"]] + top, "citation": g.get("citation", ""), "confidence": g.get("confidence", 0.5),
              "method": "rule", "produced_by": "component://flood-advisor@0.1.0", "score": g.get("confidence", 0.5),
              "sop_workflow": g.get("sop_workflow"), "created_at": _now(), "tenant": tenant})
        rec_ids.append(rid)

    _put({"schema": "platform.case", "id": case_id, "title": f"Flood response — {count} low-lying cells",
          "service": f"ds:{tenant}:solution/flood-response", "workflow": f"ds:{tenant}:workflow/flood",
          "status": "active", "current_step": "decide", "opened_by": "component://flood-advisor@0.1.0",
          "opened_at": _now(), "updated_at": _now(), "subjects": top + [alert["id"]],
          "history": [
             {"at": _now(), "event": "opened", "actor": "component://flood-advisor@0.1.0", "ref": alert["id"], "summary": f"Opened from flood alert (severity {sev}) — AUTO-fired by subscription"},
             {"at": _now(), "event": "task_created", "actor": "component://flood-advisor@0.1.0", "summary": f"{len(rec_ids)} guideline-based recommendations generated"}],
          "tenant": tenant})
    _put({"schema": "platform.task", "id": task_id, "case": case_id, "verb": "decide",
          "title": f"Decide flood response — {count} low-lying cells", "subjects": top,
          "assignee": DECIDER, "workflow_step": "decide", "decision_kind": "select", "options": rec_ids,
          "actions": ["approve", "reject"], "status": "open", "created_at": _now(), "tenant": tenant})
    _notify({"actor": DECIDER, "kind": "task", "tenant": tenant,
             "title": f"Decide flood response — {count} low-lying cells",
             "body": f"Alert {sev}. {len(rec_ids)} guideline-based options await your decision.",
             "ref": task_id, "source": "flood-advisor", "actions": ["approve", "reject"]})
    print(f"[advisor] advised on {alert['id']}: {len(rec_ids)} recs + case {case_id} + decision task")
    return {"case": case_id, "task": task_id, "recommendations": rec_ids}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        return self._send(200 if self.path == "/healthz" else 404, {"status": "ok"} if self.path == "/healthz" else {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/advise":
            return self._send(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        alert = body.get("subject") or body.get("alert")
        if not alert or alert.get("schema") != "signal.alert":
            return self._send(400, {"error": "expected a signal.alert subject"})
        try:
            return self._send(201, advise(alert))
        except Exception as e:
            return self._send(500, {"error": str(e)})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--kernel"); ap.add_argument("--notify")
    a = ap.parse_args()
    if a.kernel: KERNEL = a.kernel
    if a.notify: NOTIFY = a.notify
    print(f"[advisor] flood advisor on :{a.port}  kernel={KERNEL}")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
