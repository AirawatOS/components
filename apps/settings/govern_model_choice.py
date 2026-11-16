#!/usr/bin/env python3
"""Settings — govern intelligence.model_choice: which AI model an organisation uses, per task.

One record per tenant (id ds:<tenant>:intelligence/model_choice), written by a tenant administrator in Settings → AI
model (through the app-host's /bridge/ai-models, which checks that the provider serves the model for that task and that
the tenant may use the provider: an Airawat-hosted one, or one installed in the tenant itself with its own key). The
app-host applies it to every model call it makes for that tenant, EXCEPT requests that carry sensitive data: those
always go to the Airawat-hosted model (the gateway refuses to send them anywhere else). No record = the platform
default. Tenant configuration: sensitivity "internal". Run where NET/IDN resolve (e.g. the app-host container)."""
import json, os, urllib.error, urllib.request

NET = os.environ.get("NET", "http://network:8090")
IDN = os.environ.get("IDN", "http://identity:8093")


def _post(url, body, token=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code} {e.read().decode()[:300]}"


Sn = lambda: {"type": ["string", "null"]}
PICK = {"type": ["object", "null"], "additionalProperties": False, "required": ["provider_id", "model"],
        "properties": {"provider_id": {"type": "string"}, "model": {"type": "string"}, "hosting": Sn()}}
CHOICE = {
    "type": "object", "x-kind": "entity", "x-subject": "AI model choice", "additionalProperties": False,
    "title": "intelligence.model_choice",
    "description": "Which AI model an organisation uses for each task (chat, code). Requests that carry sensitive data "
                   "always use the Airawat-hosted model, whatever is chosen here.",
    "required": ["schema", "id", "tenant"],
    "properties": {"schema": {"const": "intelligence.model_choice"}, "id": {"type": "string", "pattern": r"^ds:[a-z0-9-]+:\S+$"},
                   "tenant": {"type": "string"}, "chat": PICK, "code": PICK,
                   "updated_by": Sn(), "updated_at": Sn()},
}

if __name__ == "__main__":
    r, err = _post(IDN + "/login", {"username": "steward"})
    if err:
        raise SystemExit("login failed: " + err.split(" ")[0])
    tok = r["access_token"]
    print("== schema: intelligence.model_choice@0.1.0 ==")
    pr, perr = _post(f"{NET}/v1/schema-requests", {"schema_id": "intelligence.model_choice", "version": "0.1.0", "definition": CHOICE,
                     "x_kind": "entity", "sensitivity": "internal",
                     "justification": "Settings → AI model: the organisation's model per task."}, token=tok)
    print("  propose:", (pr or {}).get("status", "ok") if (not perr or "exists" in perr.lower()) else perr)
    dr, derr = _post(f"{NET}/v1/schema-requests/decide", {"schema_id": "intelligence.model_choice", "version": "0.1.0", "decision": "approve"}, token=tok)
    print("  approve:", ("OK " + dr["published_schema"]) if dr else ("ERR " + str(derr)))
