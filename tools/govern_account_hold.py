#!/usr/bin/env python3
"""Govern `platform.account_hold` — a tenant-admin "disable this login" flag, enforced by the app-host at session
creation (_login_session). Lives in the airawat platform registry (like platform.app_client), written/read by the
platform service principal. Permissive (additionalProperties:true). Run on the network host:
    cat components/tools/govern_account_hold.py | ssh <host> 'docker exec -i airawat-network python -'
"""
import os, json, urllib.request
NET = os.environ.get("NETWORK_URL", "http://localhost:8090")
DEFINITION = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "platform.account_hold",
    "type": "object",
    "description": ("A hold on a user's login: when disabled=true the app-host refuses to open a session for that "
                    "(tenant, username), even though the IdP would still mint a token. Set by a tenant admin, "
                    "own-tenant only. Reversible (disabled=false re-enables)."),
    "properties": {
        "target_tenant": {"type": "string", "description": "The tenant the held user belongs to."},
        "username":      {"type": "string"},
        "subject":       {"type": "string", "description": "user://<tenant>/<username>."},
        "disabled":      {"type": "boolean", "description": "true = login blocked; false = active."},
        "reason":        {"type": "string"},
        "held_by":       {"type": "string", "description": "the admin sub that set it."},
        "held_at":       {"type": "string"},
    },
    "required": ["target_tenant", "username", "disabled"],
    "additionalProperties": True,
    "x-subject": "account",
}
def main():
    body = {"schema_id": "platform.account_hold", "version": "0.1.0", "definition": DEFINITION, "sensitivity": "internal"}
    req = urllib.request.Request(NET + "/v1/schemas", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    print(json.dumps(json.loads(urllib.request.urlopen(req, timeout=10).read() or b"{}"), indent=2))
if __name__ == "__main__":
    main()
