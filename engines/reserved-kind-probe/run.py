#!/usr/bin/env python3
"""reserved-kind-probe — attempts the governance.role_assignment write it declared (and was granted). Prints the verdict."""
import json, os, time, urllib.error, urllib.request
API = os.environ.get("AIRAWAT_API", "http://localhost:8080"); TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("TENANT") or ""; RUN = os.environ.get("PROBE_RUN") or "default"
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
oid = f"ds:{TENANT}:governance/role_assignment/reserved-probe-{RUN}"
for label, scope, schema in (("declared-scope", "governance.role_assignment", "governance.role_assignment"),):
    req = urllib.request.Request(API + "/v1/write", data=json.dumps({"scope": scope, "id": oid,
        "object": {"schema": schema, "id": oid, "role": "platform.tenant-admin", "principal": f"component:{os.environ.get('COMPONENT_ID', 'reserved-kind-probe')}", "granted_at": NOW},
        "valid_time": {"from": NOW}, "asserted_at": NOW}).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    try:
        print("RESERVED_RESULT accepted", urllib.request.urlopen(req, timeout=60).read().decode()[:160])
    except urllib.error.HTTPError as e:
        print("RESERVED_RESULT refused", e.code, e.read().decode()[:220])
