#!/usr/bin/env python3
"""Govern the model-evaluation schemas — intelligence.eval_case, intelligence.eval_run, intelligence.eval_result — in the
governed `intelligence` domain: schema-request → decide by the standards-body steward. New schemas; idempotent.
    docker exec -i <app-host> python3 - < govern_eval.py
"""
import json, os, urllib.error, urllib.request

NETWORK = os.environ.get("NETWORK_URL", "http://network:8090")
IDENTITY = os.environ.get("IDENTITY_URL", "http://identity:8093")
STEWARD_USER = os.environ.get("STEWARD_USER", "steward")
VER = "0.1.0"
IDP = r"^ds:[a-z0-9-]+:\S+$"
S, SN, N, IN, B = {"type": "string"}, {"type": ["string", "null"]}, {"type": ["number", "null"]}, {"type": ["integer", "null"]}, {"type": ["boolean", "null"]}
O, A = {"type": ["object", "null"]}, {"type": ["array", "null"]}


def defn(sid, subject, desc, fields, required):
    props = {"schema": {"const": sid}, "id": {"type": "string", "pattern": IDP}, "tenant": S}
    props.update(fields)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://airawat.org/domain/intelligence/0001/%s.schema.json" % sid,
            "title": sid, "type": "object", "x-kind": "record", "x-subject": subject, "x-layer": "L1",
            "description": desc, "additionalProperties": False,
            "required": ["schema", "id", "tenant"] + list(required), "properties": props}


SCHEMAS = [
    ("intelligence.eval_case", "eval_case",
     "One test case for a language-model task: the input EXACTLY as the platform sends it (messages + answer budget), the "
     "task it exercises, how to score an answer (scorer + expected answer, which may be empty until someone labels it), "
     "its suite, tags, where it came from (a document section, an LLM receipt, a person) and whether it may leave the "
     "tenant's hardware (public) or not (private). Run by engine/model-eval; managed in the Model Lab app.",
     {"suite": S, "task": S, "title": SN, "messages": {"type": "array"}, "max_tokens": IN,
      "scorer": {"enum": ["norms", "json_fields", "contains", "exact", "grounded"]},
      "expected": O, "labelled": B, "source": O, "tags": A,
      "visibility": {"enum": ["public", "private"]}, "status": {"enum": ["draft", "active", "retired"]},
      "created_by": SN, "created_at": SN, "updated_at": SN},
     ("suite", "task", "messages", "scorer", "visibility", "status")),
    ("intelligence.eval_run", "eval_run",
     "One evaluation run: a suite of eval_case records sent to a list of models through the model gateway (the same path "
     "production uses), with its status and a per-model summary (cases, answered, empty, valid, grounded, score, average "
     "latency and tokens). Requested from the Model Lab app; executed by engine/model-eval.",
     {"suite": S, "models": {"type": "array"}, "status": {"enum": ["pending", "running", "done", "failed"]},
      "requested_by": SN, "requested_at": SN, "started_at": SN, "finished_at": SN, "summary": O, "error": SN, "note": SN},
     ("suite", "models", "status")),
    ("intelligence.eval_result", "eval_result",
     "One case × model result of an eval_run: the model's raw output (truncated), the model the gateway reports it used, "
     "the score (0–1) and the scorer's metrics, latency, token usage and any error.",
     {"run_ref": S, "case_ref": S, "suite": SN, "model": S, "served_by": SN, "output": SN, "score": N, "metrics": O,
      "latency_ms": IN, "usage": O, "error": SN, "at": SN},
     ("run_ref", "case_ref", "model")),
]


def call(url, body=None, token=None):
    hdr = {"Content-Type": "application/json"}
    if token:
        hdr["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr), timeout=40).read() or b"{}")


def main():
    tok = call(IDENTITY + "/login", {"username": STEWARD_USER})["access_token"]
    for sid, subject, desc, fields, req in SCHEMAS:
        try:
            call(NETWORK + "/v1/schema-requests", {"schema_id": sid, "version": VER, "definition": defn(sid, subject, desc, fields, req),
                 "sensitivity": "internal", "justification": "Model Lab: test language models on the platform's own tasks before routing them."}, tok)
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise SystemExit("FAIL (%s) %s: %s" % (e.code, sid, (e.read() or b"")[:300].decode(errors="replace")))
            print("  %s: %s" % (sid, (e.read() or b"")[:200].decode(errors="replace")))
        try:
            call(NETWORK + "/v1/schema-requests/decide", {"schema_id": sid, "version": VER, "decision": "approve"}, tok)
            print("ok %s@%s governed" % (sid, VER))
        except urllib.error.HTTPError as e:
            print("  decide %s: %s %s" % (sid, e.code, (e.read() or b"")[:200].decode(errors="replace")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
