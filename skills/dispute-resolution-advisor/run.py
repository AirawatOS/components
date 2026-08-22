#!/usr/bin/env python3
"""Dispute Resolution Advisor — a confined skill; its only I/O is the broker."""
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
    # TODO: query facts + governance.rule, evaluate, write a cited governance.recommendation.
    print("TODO: implement Dispute Resolution Advisor skill")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
