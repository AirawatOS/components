#!/usr/bin/env python3
"""Data-connectors S2b — govern `platform.connector_runtime`, the governed record the kernel uses to DISCOVER a
warm connector's address (ADR option B: the registry record is the source of truth, not a hardcoded x-source
endpoint). The kernel writes/refreshes it after the runtime-manager launches the warm container; the resolver
reads it per query. Run:
    cat components/tools/govern_connector_runtime.py | ssh <host> 'docker exec -i -e IDENTITY_URL=http://identity:8093 airawat-network python -'
"""
import os, json, urllib.request, urllib.error
NET = os.environ.get("NETWORK_URL", "http://localhost:8090")
IDN = os.environ.get("IDENTITY_URL", "http://identity:8093")


def _tok():
    r = urllib.request.Request(IDN + "/realms/airawat/login", data=json.dumps({"username": "steward"}).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    return "Bearer " + json.loads(urllib.request.urlopen(r, timeout=10).read())["access_token"]


def _post(path, body, tok):
    req = urllib.request.Request(NET + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": tok}, method="POST")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=10).read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"err": e.code, "detail": e.read().decode()[:200]}


DEFN = {
    "$schema": "http://json-schema.org/draft-07/schema#", "title": "platform.connector_runtime", "type": "object",
    "description": "Discovery record for a WARM pooled connector runtime: where its read server is reachable, "
                   "which image/profile backs it, whether it is confined, and when it was last seen healthy. "
                   "The kernel resolver reads this to route externally-sourced (x-source) schemas; the runtime-"
                   "manager launches the container, the kernel owns this record.",
    "properties": {
        "id": {"type": "string"},
        "connector": {"type": "string", "description": "the connector component_id this runtime serves"},
        "address": {"type": "string", "description": "the /read endpoint the resolver POSTs a ConnectorQuery to"},
        "base_url": {"type": "string"},
        "container": {"type": "string", "description": "docker container name on the placement network"},
        "image": {"type": "string"},
        "profile": {"type": ["string", "null"], "description": "runtime profile backing it (null = dev base image)"},
        "confined": {"type": ["boolean", "null"],
                     "description": "true = certified image-locked + egress-confined runtime; false = dev base image"},
        "status": {"type": "string", "enum": ["healthy", "starting", "down"]},
        "launched_at": {"type": ["string", "null"], "format": "date-time"},
        "last_seen": {"type": "string", "format": "date-time"},
        "ttl_s": {"type": "number", "description": "freshness window; past it the kernel re-ensures the runtime"},
    },
    "required": ["id", "connector", "address", "status"], "additionalProperties": True,
}


def main():
    tok = _tok()
    print("schema ->", _post("/v1/schemas", {"schema_id": "platform.connector_runtime", "version": "0.1.0",
                                              "definition": DEFN, "sensitivity": "internal"}, tok))


if __name__ == "__main__":
    main()
