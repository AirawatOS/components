"""ADR-0111 L4 — the TRANSACTIONAL channel agent needs per-chat conversation STATE. Govern platform.conversation
(one record per (tenant, channel, chat_id)) so a stateless messaging webhook can run a multi-turn, schema-driven
service application ("apply for a Birth Certificate" → collect fields → account-link → submit).

Also provisions the `<tenant>-agent` least-privilege WRITER principal the host uses to persist that state for an
anonymous chat (the public principal is read-only; the citizen isn't signed in until account-link). Run in app-host.
"""
import json, os, urllib.request as u, urllib.error

KERNEL = os.environ.get("KERNEL", "http://airawat-kernel:8080")
NET = os.environ.get("NET", "http://airawat-network:8090")
IDN = os.environ.get("IDN", "http://airawat-identity:8093")
TENANTS = (os.environ.get("AGENT_TENANTS") or "nashik").split(",")


def login(user, tenant):
    r = u.Request(IDN + "/login", data=json.dumps({"username": user, "tenant": tenant}).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(u.urlopen(r, timeout=15).read())["access_token"]


def post(url, body, tok, label):
    r = u.Request(url, data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok}, method="POST")
    try:
        print(label, "->", json.loads(u.urlopen(r, timeout=30).read() or b"{}"))
        return True
    except urllib.error.HTTPError as e:
        print(label, "-> ERR", e.code, e.read().decode()[:200]); return False


NS = lambda: {"type": ["string", "null"]}
NO = lambda: {"type": ["object", "null"]}
ID = {"type": "string", "pattern": "^ds:[a-z0-9-]+:\\S+$"}
defn = {"type": "object", "title": "platform.conversation", "x-kind": "entity",
        "required": ["schema", "id", "tenant", "channel", "chat_id"],
        "properties": {"id": ID, "schema": {"const": "platform.conversation"}, "tenant": {"type": "string"},
            "channel": {"type": "string", "description": "telegram | web | whatsapp | ivr"},
            "chat_id": {"type": "string", "description": "the channel-native conversation id"},
            "citizen": NS(),                 # bound civic identity sub after account-link; null until linked
            "service_id": NS(),              # the service being applied for (null = idle / Q&A)
            "step": NS(),                    # idle | collecting | confirm | linking | submitted | done
            "collected": NO(),               # the request_schema fields gathered so far
            "pending_field": NS(),           # the field currently being asked
            "last_reply": NS(),              # last agent reply (observability / resume)
            "txn": NS(), "case": NS(),       # set on submit
            "updated_at": NS(), "prov": NO()},
        "additionalProperties": False,
        "description": "Per-chat conversation state for the channel-agnostic transactional service agent (ADR-0111). "
                       "One record per (tenant, channel, chat_id); drives multi-turn, schema-driven service applications."}


def main():
    stok = login("steward", "airawat")
    post(NET + "/v1/schemas", {"schema_id": "platform.conversation", "version": "0.1.0",
                               "definition": defn, "sensitivity": "internal"}, stok, "govern platform.conversation@0.1.0")


if __name__ == "__main__":
    main()
