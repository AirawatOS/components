#!/usr/bin/env python3
"""Govern knowledge.ingest_request — the request that adds a document to a tenant's knowledge base and carries its
progress through the scheduled pipeline (doc-ingest → doc-chunk-version → embed-chunks → rule-extract). New schema in
the governed `knowledge` domain: schema-request → decide by the standards-body steward. Idempotent.
    docker exec -i <app-host> python3 - < govern_ingest.py
"""
import json, os, urllib.error, urllib.request

NETWORK = os.environ.get("NETWORK_URL", "http://network:8090")
IDENTITY = os.environ.get("IDENTITY_URL", "http://identity:8093")
STEWARD_USER = os.environ.get("STEWARD_USER", "steward")
SID, VER = "knowledge.ingest_request", "0.1.0"
IDP = r"^ds:[a-z0-9-]+:\S+$"
S, SN, IN = {"type": "string"}, {"type": ["string", "null"]}, {"type": ["integer", "null"]}

DEFN = {"$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://airawat.org/domain/knowledge/0001/%s.schema.json" % SID,
        "title": SID, "type": "object", "x-kind": "record", "x-subject": "ingest_request", "x-layer": "L1",
        "description": "A request to add one document to the tenant's knowledge base — the original already in the filestore "
                       "(file_id) or an official URL — with its title, issuer, class, legal force and an optional norm-extraction "
                       "focus. driver/doc-ingest processes it on a schedule and records its progress: pending → extracting → "
                       "acquired → sectioned → indexed → rules_proposed → done, or failed (stage + reason; retried up to 3 "
                       "attempts). The extracted norms are PROPOSED governance.rule records awaiting an administrator's approval.",
        "additionalProperties": False,
        "required": ["schema", "id", "tenant", "title", "source_kind", "status"],
        "properties": {
            "schema": {"const": SID}, "id": {"type": "string", "pattern": IDP}, "tenant": S, "title": S,
            "issuer": SN, "doc_class": SN, "legal_force": SN, "citation_style": SN, "jurisdiction": {"type": ["object", "null"]},
            "source_kind": {"enum": ["file", "url"]}, "file_id": SN, "url": SN, "filename": SN, "format": SN,
            "focus": SN, "max_extract": IN,
            "status": {"enum": ["pending", "extracting", "acquired", "sectioned", "indexed", "rules_proposed", "done", "failed"]},
            "stage": SN, "error": SN, "attempts": IN, "doc_id": SN, "doc_ref": SN,
            "counts": {"type": ["object", "null"]},
            "requested_at": SN, "requested_by": SN, "updated_at": SN}}


def call(url, body=None, token=None):
    hdr = {"Content-Type": "application/json"}
    if token:
        hdr["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr), timeout=40).read() or b"{}")


def main():
    tok = call(IDENTITY + "/login", {"username": STEWARD_USER})["access_token"]
    try:
        call(NETWORK + "/v1/schema-requests", {"schema_id": SID, "version": VER, "definition": DEFN, "sensitivity": "internal",
             "justification": "Knowledge ingest as a scheduled job: upload → request → processed on a schedule, progress on the request."}, tok)
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise SystemExit("FAIL (%s): %s" % (e.code, (e.read() or b"")[:300].decode(errors="replace")))
        print("  %s: %s" % (SID, (e.read() or b"")[:200].decode(errors="replace")))
    try:
        call(NETWORK + "/v1/schema-requests/decide", {"schema_id": SID, "version": VER, "decision": "approve"}, tok)
        print("ok %s@%s governed" % (SID, VER))
    except urllib.error.HTTPError as e:
        print("  decide: %s %s" % (e.code, (e.read() or b"")[:200].decode(errors="replace")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
