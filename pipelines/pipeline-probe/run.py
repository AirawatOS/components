#!/usr/bin/env python3
"""pipeline-probe — conformance fixture for the scheduler-owned pipeline contract (slice-1c-iv step 3).
PHASE=pre: write a 'started' inbox item, declare the member plan. PHASE=post: write a 'done' item with results.
The only I/O is the broker under RUN_TOKEN; members are run by the SCHEDULER, never from here."""
import json, os, time, urllib.request, urllib.error
API = os.environ.get("AIRAWAT_API", "http://localhost:8080"); TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("TENANT") or ""; PHASE = (os.environ.get("PHASE") or "pre").lower()
RUN = os.environ.get("PROBE_RUN") or "default"
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
if not TENANT:
    raise SystemExit("FATAL: TENANT required")

def write(oid, obj):
    req = urllib.request.Request(API + "/v1/write", data=json.dumps({"scope": "platform.inbox_item", "id": oid,
        "object": obj, "valid_time": {"from": NOW}, "asserted_at": NOW}).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    try:   # best-effort + REPORTED: the fixture proves EXECUTION; the broker's verdict is printed, never hidden
        r = json.loads(urllib.request.urlopen(req, timeout=60).read()); print("WRITE_RESULT ok", oid); return r
    except urllib.error.HTTPError as e:
        print("WRITE_RESULT refused", e.code, e.read().decode()[:200]); return None

def item(tag, title, body):
    oid = f"ds:{TENANT}:platform/inbox_item/pipeline-probe-{RUN}-{tag}"
    write(oid, {"schema": "platform.inbox_item", "id": oid, "actor": f"role://{TENANT}/platform.tenant-admin",
                "kind": "notification", "title": title, "body": body, "status": "unread", "created_at": NOW})
    return oid

if PHASE == "pre":
    item("started", "pipeline-probe started", "pre phase")
    print("AIROS_PIPELINE_PLAN " + json.dumps([
        {"label": "step a", "member": "engine/pipeline-probe-step", "env": {"STEP": "a", "PROBE_RUN": RUN}},
        {"label": "step b", "member": "engine/pipeline-probe-step", "env": {"STEP": "b", "PROBE_RUN": RUN}}]))
elif PHASE == "post":
    res = os.environ.get("STEP_RESULTS") or "[]"
    item("done", f"pipeline-probe done {os.environ.get('STEPS_OK')}/{os.environ.get('STEPS_TOTAL')}", res)
else:
    raise SystemExit(f"FATAL: unknown PHASE {PHASE}")
