#!/usr/bin/env python3
"""MSRTC Transit Actuator — the machine-actor stand-in for the connected bus-scheduling SYSTEM.

Polls for crowd-safety orders addressed to it (platform.task where assignee = system://<tenant>/
msrtc-scheduler and status != done), "actuates" them (here: simulates re-timing / holding buses on the
affected routes), marks each task DONE with a completion receipt, and rolls the case up (closes it when
every sibling task is done). This is the no-human-in-the-loop fulfilment path — the order routes to the
system, which actuates and reports back into the SAME case as the human tasks. All I/O is via the broker.
(In production this runs as the transport system holding its own credential; here it is a certified
component fulfilling orders assigned to the msrtc-scheduler actor.)
"""
import json, os, time, urllib.request

API   = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]

def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())

def context():
    req = urllib.request.Request(API + "/v1/context", headers={"X-Run-Token": TOKEN})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

def main():
    T = context().get("tenant") or os.environ.get("WRITE_TENANT", "kmc")
    sysactor = f"system://{T}/msrtc-scheduler"
    ts = os.environ.get("NOW") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tasks = [r.get("object") or r for r in call("/v1/query", {"scope": "platform.task", "limit": 50000}).get("results", [])]
    todo = [t for t in tasks if t.get("assignee") == sysactor and t.get("status") != "done"]
    if not todo:
        print("transit-actuator: no open system orders for", sysactor); return 0
    done_cases = 0
    for t in todo:
        held = 14   # buses held / re-timed (simulated actuation)
        upd = {**t, "status": "done", "done_at": ts, "completed_by": sysactor}
        call("/v1/write", {"scope": "platform.task", "id": t["id"], "object": upd})
        print(f"  ← order '{t.get('title','')}' ACTUATED: re-timed {held} buses on Ramkund routes; arrivals throttled -35%.")
        # roll the case up: close it when every sibling task is done
        case_id = t.get("case")
        if not case_id:
            continue
        sibs = [x for x in tasks if x.get("case") == case_id]
        remaining = [s for s in sibs if s.get("id") != t["id"] and s.get("status") != "done"]
        if not remaining:
            case = next((c.get("object") or c for c in
                         call("/v1/query", {"scope": "platform.case", "limit": 50000}).get("results", [])
                         if (c.get("object") or c).get("id") == case_id), None)
            if case:
                call("/v1/write", {"scope": "platform.case", "id": case_id,
                                   "object": {**case, "status": "closed", "current_step": "closed", "closed_at": ts}})
                done_cases += 1
                print(f"     ↳ case {case_id.split('/')[-1]} CLOSED (all orders fulfilled)")
    print(f"transit-actuator: actuated {len(todo)} order(s), closed {done_cases} case(s) in {T}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
