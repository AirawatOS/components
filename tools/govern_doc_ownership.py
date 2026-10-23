#!/usr/bin/env python3
"""ADR-0103 S1 (first noun) — flip `doc.document` to `x-ownership: user`.

Makes each doc owned by its CREATOR (the kernel already stamps `owner` from the verified identity on create,
and the schema already REQUIRES `owner`, so every existing doc keeps its creator as owner). After this, a
non-owner in the same tenant can no longer read a doc by id without an `access.grant` the owner issued — and
owner-granted / public (`anyone`/`link:`) sharing + the Opener now govern real Docs data.

Idempotent: resolves the current definition, sets the facet, republishes the same version. Run on the network
host:  cat components/tools/govern_doc_ownership.py | ssh <host> 'docker exec -i airawat-network python -'
"""
import os, json, urllib.request

NET = os.environ.get("NETWORK_URL", "http://localhost:8090")
SID, VER = "doc.document", "0.1.0"


def main():
    r = urllib.request.Request(NET + "/v1/schemas/resolve",
                               data=json.dumps({"schema_id": SID}).encode(),
                               headers={"Content-Type": "application/json"})
    rec = json.load(urllib.request.urlopen(r, timeout=10))
    defn = rec.get("definition") or rec.get("schema") or {}
    if not defn.get("properties"):
        raise SystemExit(f"refusing: resolved {SID} has no properties — got {list(rec)[:6]}")
    if "owner" not in (defn.get("properties") or {}):
        raise SystemExit("refusing: doc.document has no 'owner' property to key ownership on")

    before = defn.get("x-ownership") or defn.get("x_ownership")
    defn["x-ownership"] = "user"                 # owner = object.owner (the default x-owner-field)

    body = {"schema_id": SID, "version": VER, "definition": defn,
            "sensitivity": rec.get("sensitivity", "internal")}
    pr = urllib.request.Request(NET + "/v1/schemas", data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
    out = json.loads(urllib.request.urlopen(pr, timeout=10).read() or b"{}")
    print(json.dumps({"x_ownership_before": before, "x_ownership_after": "user", "published": out}, indent=2))


if __name__ == "__main__":
    main()
