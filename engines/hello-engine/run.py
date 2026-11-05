#!/usr/bin/env python3
"""hello-engine — the smallest ENGINE that runs through the real pipeline.

Its ONLY I/O is a broker READ of demo.ping (what hello-dev writes), under the run-token in the clean
environment. It reads its tenant from the broker context (never hardcoded), counts the demo.ping facts, and
prints a RESULT line. Read-only; no egress; no secrets.

  Env: AIRAWAT_API · RUN_TOKEN
"""
import json, os, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def context():
    req = urllib.request.Request(API + "/v1/context", headers={"X-Run-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main():
    ctx = context()
    tenant = ctx.get("tenant")
    if not tenant:
        raise SystemExit("FATAL: broker context carries no tenant")
    rows = call("/v1/query", {"filter": {"schema": "demo.ping"}, "limit": 500}).get("results", [])
    print("RESULT: " + json.dumps({"tenant": tenant, "demo_ping_read": len(rows)}))
    print(f"hello-engine: read {len(rows)} demo.ping fact(s) in {tenant} through the broker — OK")


if __name__ == "__main__":
    main()
