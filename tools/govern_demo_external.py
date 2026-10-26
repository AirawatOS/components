#!/usr/bin/env python3
"""Data-connectors S1 test fixture — govern a throwaway `demo.external` schema that declares `x-source` with
policy:route, so the kernel resolver's connector branch fires for it (and ONLY for it). Points at a canned
connector-serve endpoint reachable on the docker network (conn-test:9999). Run on the network host:
    cat components/tools/govern_demo_external.py | ssh <host> 'docker exec -i -e IDENTITY_URL=http://identity:8093 airawat-network python -'
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
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "demo.external",
    "type": "object",
    "description": "Throwaway schema for the data-connectors S1 resolver test — resolved live via a connector.",
    "properties": {
        "id": {"type": "string"}, "ward": {"type": "integer"}, "status": {"type": "string"},
    },
    "required": ["id"],
    "additionalProperties": True,
    # S2b: NO endpoint — the kernel DISCOVERS the warm demo-connector's address from the governed
    # platform.connector_runtime record (ADR option B). Route placement fires for this schema only.
    "x-source": {
        "connector": "ds:airawatdeveloper:connector/demo-connector",
        "policy": "route",
        "deadline_s": 5,
    },
}


def main():
    tok = _tok()
    print("domain ->", _post("/v1/domains", {"domain": "demo", "label": "Demo"}, tok).get("published", {}).get("id", "(exists)"))
    print("schema ->", _post("/v1/schemas", {"schema_id": "demo.external", "version": "0.1.0",
                                             "definition": DEFN, "sensitivity": "internal"}, tok))


if __name__ == "__main__":
    main()
