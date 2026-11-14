#!/usr/bin/env python3
"""Govern the narration schemas — media.narration_clip and media.narration_request — schema-request → decide, filed and approved by
the standards-body steward. New schemas (no version bump), so the kernel's per-id validator cache needs no restart.
Idempotent.
    docker exec -i <app-host> python3 - < govern_narration.py
"""
import json, os, urllib.error, urllib.request

NETWORK = os.environ.get("NETWORK_URL", "http://network:8090")
IDENTITY = os.environ.get("IDENTITY_URL", "http://identity:8093")
STEWARD_USER = os.environ.get("STEWARD_USER", "steward")
VER = "0.1.0"
IDP = r"^ds:[a-z0-9-]+:\S+$"
S, SN, N, I = {"type": "string"}, {"type": ["string", "null"]}, {"type": ["number", "null"]}, {"type": ["integer", "null"]}
KEY = {"type": "string", "pattern": "^[0-9a-f]{24}$"}
VOICE = {"language": S, "voice": S, "model": S, "pace": {"type": "number"}}


def call(url, body=None, token=None):
    hdr = {"Content-Type": "application/json"}
    if token:
        hdr["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr), timeout=40).read() or b"{}")


def defn(sid, subject, desc, fields, required):
    props = {"schema": {"const": sid}, "id": {"type": "string", "pattern": IDP}, "tenant": S, "key": KEY,
             "text": {"type": "string", "maxLength": 2500}}
    props.update(VOICE); props.update(fields)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://airawat.org/domain/media/0001/%s.schema.json" % sid,
            "title": sid, "type": "object", "x-kind": "record", "x-subject": subject, "x-layer": "L2",
            "description": desc, "additionalProperties": False,
            "required": ["schema", "id", "tenant", "key", "text", "language", "voice", "model", "pace"] + list(required),
            "properties": props}


SCHEMAS = [
    ("media.narration_clip", "narration_clip",
     "Spoken audio for one line of text, generated once and kept: the text, the voice settings (language, voice, model, "
     "pace) and the audio file in the filestore. key = sha-256 of 'language|voice|model|pace|text' (first 24 hex), so a "
     "player can look a line up without a query by text. Derived (driver/narration).",
     {"provider": S, "file_id": S, "media_type": S, "size_bytes": I, "duration_s": N, "created_at": S, "prov": {"type": "object"}},
     ("provider", "file_id", "media_type", "created_at")),
    ("media.narration_request", "narration_request",
     "A request to narrate one line that has no clip yet (filed by a player); driver/narration synthesises it and marks it "
     "done (with clip_ref) or failed (with the reason; retried up to 3 attempts).",
     {"status": {"enum": ["pending", "done", "failed"]}, "requested_at": SN, "requested_by": SN, "updated_at": SN,
      "clip_ref": SN, "error": SN, "attempts": I},
     ("status",)),
]


def main():
    tok = call(IDENTITY + "/login", {"username": STEWARD_USER})["access_token"]
    for sid, subject, desc, fields, req in SCHEMAS:
        try:
            call(NETWORK + "/v1/schema-requests", {"schema_id": sid, "version": VER, "definition": defn(sid, subject, desc, fields, req),
                 "sensitivity": "internal", "justification": "Narration: text-to-speech generated once, kept, and reused by any app."}, tok)
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
