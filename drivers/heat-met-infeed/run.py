#!/usr/bin/env python3
"""Met Heat Data Infeed — a confined driver; its only I/O is the broker."""
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
    # 1) FETCH from the external source via the broker (egress-gated). OpenStreetMap roads via Overpass:
        q = '[out:json];way["highway"](AREA_BBOX);out geom;'
        r = call("/v1/fetch", {"url": "https://example.com/api/interpreter", "method": "POST", "body": q})
    # 2) PARSE r into met.forecast records (one per road/way).
    # 3) WRITE each via the broker:
        call("/v1/write", {"scope": "met.forecast", "id": f"ds:{T}:...", "object": {...}})
    print("TODO: implement Met Heat Data Infeed — fetch example.com -> write met.forecast")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
