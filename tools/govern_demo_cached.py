#!/usr/bin/env python3
"""Data-connectors S2 test fixture — govern `demo.cached` with x-source policy:cache (ttl 300s, serve_stale),
so the kernel resolver serves a TTL pull-through cache in front of the connector. Run:
    cat components/tools/govern_demo_cached.py | ssh <host> 'docker exec -i -e IDENTITY_URL=http://identity:8093 airawat-network python -'
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
    "$schema": "http://json-schema.org/draft-07/schema#", "title": "demo.cached", "type": "object",
    "description": "Throwaway schema for the data-connectors S2 cache test — cached projection over a connector.",
    "properties": {"id": {"type": "string"}, "ward": {"type": "integer"}, "status": {"type": "string"}},
    "required": ["id"], "additionalProperties": True,
    "x-source": {
        "connector": "ds:airawatdeveloper:connector/rest-connector",
        "policy": "cache",
        "endpoint": "http://conn-test:9999/read",
        "deadline_s": 5,
        "cache": {"ttl_s": 300, "on_source_down": "serve_stale"},
        "residency": {"may_cache": True},
    },
}
def main():
    tok = _tok()
    print("schema ->", _post("/v1/schemas", {"schema_id": "demo.cached", "version": "0.1.0",
                                             "definition": DEFN, "sensitivity": "internal"}, tok))
if __name__ == "__main__":
    main()
