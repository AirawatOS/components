#!/usr/bin/env python3
"""pipeline-probe-step — a pipeline member fixture: writes one inbox item for STEP under its own run token."""
import json, os, time, urllib.request
API = os.environ.get("AIRAWAT_API", "http://localhost:8080"); TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("TENANT") or ""; STEP = os.environ.get("STEP") or "x"; RUN = os.environ.get("PROBE_RUN") or "default"
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
oid = f"ds:{TENANT}:platform/inbox_item/pipeline-probe-{RUN}-step-{STEP}"
req = urllib.request.Request(API + "/v1/write", data=json.dumps({"scope": "platform.inbox_item", "id": oid,
    "object": {"schema": "platform.inbox_item", "id": oid, "actor": f"role://{TENANT}/platform.tenant-admin",
               "kind": "notification", "title": f"pipeline-probe step {STEP}", "status": "unread", "created_at": NOW},
    "valid_time": {"from": NOW}, "asserted_at": NOW}).encode(),
    headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
import urllib.error
try:   # best-effort + REPORTED (see pipeline-probe): the broker's verdict names THIS component's own run token
    print("WRITE_RESULT ok", json.dumps(json.loads(urllib.request.urlopen(req, timeout=60).read()))[:160])
except urllib.error.HTTPError as e:
    print("WRITE_RESULT refused", e.code, e.read().decode()[:200])
if os.environ.get("PROBE_OUT_OF_GRANT") == "1":   # slice-1e functional check: a scope this component did NOT declare
    oid2 = f"ds:{TENANT}:platform/schedule/pipeline-probe-{RUN}-outside"
    r2 = urllib.request.Request(API + "/v1/write", data=json.dumps({"scope": "platform.schedule", "id": oid2,
        "object": {"schema": "platform.schedule", "id": oid2}, "valid_time": {"from": NOW}, "asserted_at": NOW}).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    try:
        urllib.request.urlopen(r2, timeout=60).read(); print("OUTSIDE_GRANT_RESULT accepted")
    except urllib.error.HTTPError as e:
        print("OUTSIDE_GRANT_RESULT refused", e.code, e.read().decode()[:160])
leak = sorted(k for k in os.environ if k.startswith(("VAULT", "SERVICE_", "IDENTITY", "SECRETS")))
print("ENVCHECK", "CLEAN" if not leak else "LEAK " + ",".join(leak))
print(f"target {oid}")
