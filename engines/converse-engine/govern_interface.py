"""Govern the public conversational verb as a network.interface contract (ADR-0058 / ADR-0111 L4).

`platform.converse@0.1.0` — a request-modality verb the converse-engine implements. Governing it makes the
grounding contract a signed standards-catalog interface, so certify's `governed-interfaces` check enforces it and
every channel binds to the same contract. Steward-gated (airawat standards body); idempotent on re-run.
Run INSIDE a service container that can reach the network + identity (e.g. airawat-app-host).
"""
import json, os, urllib.request as u, urllib.error

NET = os.environ.get("NET", "http://airawat-network:8090")
IDN = os.environ.get("IDN", "http://airawat-identity:8093")


def login(tenant="airawat", user="steward"):
    r = u.Request(IDN + "/login", data=json.dumps({"username": user, "tenant": tenant}).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    d = json.loads(u.urlopen(r, timeout=15).read())
    return d.get("access_token") or d.get("token")


def post(url, body, tok):
    r = u.Request(url, data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok}, method="POST")
    try:
        return json.loads(u.urlopen(r, timeout=60).read() or b"{}"), None
    except urllib.error.HTTPError as e:
        return None, "%d:%s" % (e.code, e.read().decode()[:240])


DEF = {
    "modality": "request",
    "x-actor": "public",                                   # anonymous citizen on any public channel
    "description": "Answer a public question about an agency, grounded STRICTLY in that agency's governed "
                   "capability index + experience + publications — never inventing a service, figure, date, or "
                   "link. Returns a concise reply plus citations validated against real records and suggested "
                   "follow-ups. Read-only; it proposes an answer, it never acts. Called by every public channel "
                   "(web Ask widget, Telegram, WhatsApp/IVR) so provenance is enforced in one place.",
    "request": {"type": "object", "required": ["tenant", "text"], "properties": {
        "tenant": {"type": "string", "description": "the agency tenant whose governed public material answers this"},
        "text": {"type": "string", "description": "the citizen's question (<=600 chars used)"},
        "locale": {"type": "string", "enum": ["en", "hi"], "description": "reply language; default en"},
        "base": {"type": "string", "description": "deployment base URL for building capability open-links"}}},
    "response": {"type": "object", "required": ["reply"], "properties": {
        "reply": {"type": "string", "description": "2-4 sentence grounded answer"},
        "citations": {"type": "array", "items": {"type": "object", "properties": {
            "ref": {"type": "string"}, "kind": {"type": "string", "enum": ["capability", "publication"]},
            "title": {"type": "string"}, "open_url": {"type": ["string", "null"]},
            "provenance": {"type": "string"}}}, "description": "records the reply stands on (validated — never invented)"},
        "suggested": {"type": "array", "items": {"type": "string"}, "description": "up to 3 follow-up questions the material can answer"},
        "agency": {"type": "string"}}},
    "roles": [],
}


def main():
    tok = login()
    if not tok:
        raise SystemExit("could not obtain a steward token")
    r, err = post(NET + "/v1/interfaces", {"interface_id": "platform.converse", "version": "0.1.0", "definition": DEF}, tok)
    print("platform.converse@0.1.0 ->", (err or r))


if __name__ == "__main__":
    main()
