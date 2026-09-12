#!/usr/bin/env python3
"""stat-reader — a certified reader for shared official-statistics series (stat.price_index).

Its ONLY I/O is a broker READ of stat.price_index. When installed on a data-provider tenant (e.g. mospi) it
receives that tenant's signed read grant; a consumer tenant then reads the provider's series THROUGH this
component via the cross-tenant aggregator lane (grant-gated). Read-only — it never writes.

  Env: TENANT · RUN_TOKEN · SOURCE_TENANT (whose series to read; default = own tenant) · INDICATOR · LIMIT
"""
import json, os, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
SOURCE_TENANT = os.environ.get("SOURCE_TENANT") or os.environ.get("TENANT", "")
INDICATOR = os.environ.get("INDICATOR", "")
LIMIT = int(os.environ.get("LIMIT", "500"))


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def main():
    body = {"filter": {"schema": "stat.price_index"}, "limit": LIMIT}
    if SOURCE_TENANT:
        body["tenant"] = SOURCE_TENANT          # cross-tenant aggregator read (grant-gated by the broker)
    res = call("/v1/query", body).get("results", [])
    rows = [r.get("object", r) for r in res]
    if INDICATOR:
        rows = [r for r in rows if r.get("indicator") == INDICATOR]
    n = len(rows)
    periods = sorted({r.get("period") for r in rows if r.get("period")})
    print("RESULT: " + json.dumps({"source_tenant": SOURCE_TENANT, "read": n,
                                   "periods": [periods[0], periods[-1]] if periods else []}))


if __name__ == "__main__":
    main()
