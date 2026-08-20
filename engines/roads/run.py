#!/usr/bin/env python3
"""Roads — a confined engine; its only I/O is the broker."""
import json, os, urllib.request
API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
T = os.environ.get("WRITE_TENANT", "kmc")

def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())

def main():
    rows = [r.get("object") or r for r in call("/v1/query", {"scope": "TODO.input", "limit": 5000}).get("results", [])]
    # TODO: compute derived facts, then call("/v1/write", {"scope": "TODO.schema", "id": ..., "object": ...})
    print("TODO: implement Roads")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
