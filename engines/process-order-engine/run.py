#!/usr/bin/env python3
"""Summons & Warrant Order Engine — a confined engine; its only I/O is the broker."""
import json, os, urllib.request
API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]

def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())

def context():
    # This run's identity — tenant + effective capabilities. /v1/context is a GET keyed by the token.
    req = urllib.request.Request(API + "/v1/context", headers={"X-Run-Token": TOKEN})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

# Write-tenant comes from the RUN, never a hardcoded default — a dev-token pins it to the developer
# sandbox, an installed run to the install tenant. This is what lets the same code test in dev and
# run in prod unchanged (INT-0004).
T = context().get("tenant") or os.environ.get("WRITE_TENANT", "kmc")

def main():
    rows = [r.get("object") or r for r in call("/v1/query", {"scope": "TODO.input", "limit": 5000}).get("results", [])]
    # TODO: compute derived facts, then call("/v1/write", {"scope": "governance.order", "id": ..., "object": ...})
    print("TODO: implement Summons & Warrant Order Engine")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
