#!/usr/bin/env python3
"""ADR-0103 S0 — give the governed `access.grant` noun a real (permissive) JSON-Schema definition.

`access.grant@0.1.0` is already governed on the network but was runtime-proposed WITHOUT a definition, so
the kernel reads its fields without validating them. This attaches a definition that:
  - REQUIRES the three load-bearing fields the kernel's _active_grant / owner-only-issuance logic reads
    (object_id, grantee, scope);
  - documents the grantee grammar, including the ADR-0103 S2a PUBLIC forms `anyone` and `link:<token>`;
  - stays a SUPERSET of every grant ever written (additionalProperties:true + envelope keys tolerated by
    the kernel validator), so publishing it can never reject an existing or in-flight grant.

Run it ON the network host (it talks to the in-cluster network authority), e.g.:
    docker exec airawat-network python - < components/tools/govern_access_grant.py
or point NETWORK_URL at the authority and run locally.
"""
import os, json, urllib.request

NETWORK_URL = os.environ.get("NETWORK_URL", "http://localhost:8090")

DEFINITION = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "access.grant",
    "type": "object",
    "description": ("A per-object capability the OWNER of a record issues to let someone else read/comment/edit "
                    "it (ADR-0103). The kernel enforces only-owner-may-grant, expiry, and revocation."),
    "properties": {
        "object_id": {"type": "string",
                      "description": "The ds:… id of the record this grant is about."},
        "grantee":   {"type": "string",
                      "description": ("WHO is admitted. Forms: a principal sub (user://tenant/sub); "
                                      "'anyone' = PUBLIC/world-readable; 'link:<token>' = anyone presenting the "
                                      "opaque token (share-by-link); 'role://<tenant>/<role>' = any holder of a role.")},
        "scope":     {"type": "string", "enum": ["view", "comment", "edit"],
                      "description": "Capability ceiling: view < comment < edit."},
        "granted_by": {"type": "string",
                       "description": "The owner who issued it — STAMPED by the kernel from the verified identity, "
                                      "never trusted from the body."},
        "reason":     {"type": "string"},
        "expires_at": {"type": "string",
                       "description": "ISO-8601 Z instant after which the grant is inert (optional)."},
        "revoked":    {"type": "boolean",
                       "description": "Set true to kill the grant immediately (append-only tombstone of capability)."},
    },
    "required": ["object_id", "grantee", "scope"],
    "additionalProperties": True,
    "x-subject": "grant",
}


def main():
    body = {"schema_id": "access.grant", "version": "0.1.0",
            "definition": DEFINITION, "sensitivity": "internal"}
    req = urllib.request.Request(NETWORK_URL + "/v1/schemas",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    out = json.loads(urllib.request.urlopen(req, timeout=10).read() or b"{}")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
