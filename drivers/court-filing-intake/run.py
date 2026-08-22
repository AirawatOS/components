#!/usr/bin/env python3
"""Court e-Filing Intake — a confined driver; its only I/O is the broker."""
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

import hashlib

def fetch(url, method="GET", body=None):
    """Egress ONLY through the broker (host allow-listed in meta.egress; keys injected server-side)."""
    return call("/v1/fetch", {"url": url, "method": method, "body": body})

def main():
    # 1) FETCH from example.com. TODO: set the real endpoint + query/params for your source.
    resp = fetch("https://example.com/")            # e.g. add ?bbox=... or a POST body
    rows = resp.get("results") or resp.get("data") or resp   # TODO: pick the list your API returns

    written = 0
    for r in (rows if isinstance(rows, list) else []):
        # 2) MAP each source record to a platform.case fact. TODO: fill the fields your schema requires.
        key = str(r.get("id") or r)
        fid = f"ds:{T}:platform/case/" + hashlib.sha1(key.encode()).hexdigest()[:16]
        obj = {"schema": "platform.case", "id": fid, "tenant": T}   # TODO: + the measured values from r
        call("/v1/write", {"scope": "platform.case", "id": fid, "object": obj})
        written += 1
    print(f"Court e-Filing Intake: wrote {written} platform.case records to tenant {T}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
