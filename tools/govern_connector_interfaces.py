#!/usr/bin/env python3
"""Data-connectors S0 — govern the two read-side interfaces every connector implements: `data.read@1.0` and
`data.write@1.0`. Modeled on components/engines/funding-engine/govern_interfaces.py — POST /v1/interfaces as the
airawat standards body (steward-gated). Idempotent. Run on the network host:
    cat components/tools/govern_connector_interfaces.py | ssh <host> 'docker exec -i airawat-network python -'
"""
import os, json, urllib.request, urllib.error
NET = os.environ.get("NETWORK_URL", "http://localhost:8090")
IDN = os.environ.get("IDENTITY_URL", "http://identity:8093")


def _steward_token():
    """Interfaces are steward-gated: log in as the airawat standards-body steward (same as funding-engine's
    govern script) and return a bearer header."""
    req = urllib.request.Request(IDN + "/realms/airawat/login", data=json.dumps({"username": "steward"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    tok = json.loads(urllib.request.urlopen(req, timeout=10).read())["access_token"]
    return "Bearer " + tok

# A governed query/page are polymorphic over domain nouns, so the request/response schemas describe the ENVELOPE
# (schema/tenant/where/paging) and leave `rows` open ("*") — any governed noun satisfies. Authz is per-schema
# (x-source + the resolver), not on the verb, so roles:[].
QUERY = {
    "type": "object",
    "required": ["schema", "tenant"],
    "properties": {
        "schema": {"type": "string", "description": "the governed noun being resolved"},
        "tenant": {"type": "string"},
        "where": {"type": "array", "items": {"type": "object",
                  "properties": {"field": {"type": "string"}, "op": {"type": "string"}, "value": {}}}},
        "projection": {"type": "array", "items": {"type": "string"}},
        "sort": {"type": "array", "items": {"type": "object"}},
        "limit": {"type": "integer"},
        "cursor": {"type": ["string", "null"]},
        "as_of": {"type": "string"},
        "require": {"type": "string", "enum": ["complete", "best_effort"]},
    },
    "additionalProperties": True,
}
PAGE = {
    "type": "object",
    "required": ["rows"],
    "properties": {
        "rows": {"type": "array", "items": {"type": "object"}},
        "cursor": {"type": ["string", "null"]},
        "observed_at": {"type": "string", "description": "when the source actually answered (freshness)"},
        "completeness": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["complete", "partial", "unavailable"]},
            "reason": {"type": "string"}, "evaluated_rows": {"type": "integer"}}},
        "source_ref": {"type": "string"},
    },
    "additionalProperties": True,
}
INTERFACES = {
    "data.read": {
        "version": "0.1.0", "modality": "request",
        "description": ("Resolve a governed schema by reading from a source a connector adapts. The resolver sends "
                        "a governed query; the connector returns a governed page (rows + cursor + freshness + "
                        "completeness). The one read-side contract every connector implements."),
        "request": QUERY, "response": PAGE, "roles": [],
    },
    "data.write": {
        "version": "0.1.0", "modality": "request",
        "description": ("Optional write-back: apply a governed record to the source a connector adapts. Secondary "
                        "to data.read; defined so a connector MAY accept writes where the source permits."),
        "request": {"type": "object", "required": ["schema", "tenant", "object"],
                    "properties": {"schema": {"type": "string"}, "tenant": {"type": "string"},
                                   "object": {"type": "object"}}, "additionalProperties": True},
        "response": {"type": "object", "properties": {"ok": {"type": "boolean"}, "id": {"type": "string"}},
                     "additionalProperties": True},
        "roles": [],
    },
}


def main():
    stok = _steward_token()
    # the `data` domain must be governed before `data.read`/`data.write` interfaces can be published (idempotent)
    dreq = urllib.request.Request(NET + "/v1/domains", data=json.dumps({"domain": "data", "label": "Data Access"}).encode(),
                                  headers={"Content-Type": "application/json", "Authorization": stok}, method="POST")
    try:
        print("domain data ->", json.loads(urllib.request.urlopen(dreq, timeout=10).read() or b"{}").get("published", {}).get("id", "ok"))
    except urllib.error.HTTPError as e:
        print("domain data (exists/err)", e.code, e.read().decode()[:120])
    for name, d in INTERFACES.items():
        body = {"interface_id": name, "version": d["version"], "definition": {
            "title": name, "modality": d["modality"], "description": d["description"],
            "request": d["request"], "response": d["response"], "roles": d["roles"]}}
        req = urllib.request.Request(NET + "/v1/interfaces", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": stok}, method="POST")
        try:
            print(name, "->", json.loads(urllib.request.urlopen(req, timeout=10).read() or b"{}"))
        except urllib.error.HTTPError as e:
            print(name, "ERR", e.code, e.read().decode()[:200])


if __name__ == "__main__":
    main()
