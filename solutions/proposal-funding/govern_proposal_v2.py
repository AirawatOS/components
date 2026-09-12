#!/usr/bin/env python3
"""ADR-0071 S1 — extend `project.proposal` to 0.2.0 with the five funding-pivot fields that turn a project
baseline into a fundable proposal, WITHOUT changing anything existing (all five are optional → backward-compatible):

  answers      — the governance.recommendation / welfare.priority ids this proposal responds to (the *why*)
  evidence     — signal.report ids + documents (citizen/JE ground-truth: media + GPS)
  site         — the jurisdiction / cells it acts on
  intervention — the chosen lever (from the ADR-0069 Step-2 catalog): kind, cost, environmental draw
  ask          — the funding request: amount, mode (addressed|open|self), target program

Re-proposes the FULL schema (existing fields verbatim + the five new) at 0.2.0; existing 0.1.0 proposals still
validate. Propose as devrick → approve as steward. LOCAL by default; override AIROS_APP/NET/IDN for EC2."""
import os, json, urllib.request, urllib.error, http.cookiejar

APP = os.environ.get("AIROS_APP", "http://localhost:8099")
NET = os.environ.get("AIROS_NET", "http://localhost:8090")
IDN = os.environ.get("AIROS_IDN", "http://localhost:8093")
ID_PAT = r"^ds:[a-z0-9-]+:\S+$"

def _post(url, body, token=None, cj=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    if token: req.add_header("Authorization", "Bearer " + token)
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(op.open(req, timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:240]}"

S = lambda: {"type": "string"}
Sn = lambda: {"type": ["string", "null"]}
N = lambda: {"type": "number"}
Nn = lambda: {"type": ["number", "null"]}
On = lambda: {"type": ["object", "null"]}
A = lambda: {"type": "array", "items": {"type": "object"}}
An = lambda: {"type": ["array", "null"]}

DEFN = {
  "type": "object", "x-kind": "record", "additionalProperties": False,
  "title": "project.proposal",
  "description": ("A PROJECT BASELINE (versioned proposal): the embedded results chain outcomes→outputs→"
                  "milestones/deliverables and the outlay that sums to the budget. Approval = a governance.decision. "
                  "0.2.0 adds the ADR-0071 funding-pivot fields (answers/evidence/site/intervention/ask) so a "
                  "baseline can also be a fundable proposal carried from a recommendation to a funder — all optional."),
  "required": ["schema", "id", "project", "version", "status"],
  "properties": {
    "schema": {"const": "project.proposal"}, "id": {"type": "string", "pattern": ID_PAT},
    "project": S(), "version": N(),
    "status": {"enum": ["draft", "submitted", "approved", "superseded", "rejected"]},
    "title": Sn(), "summary": Sn(),
    "outcomes": A(), "outputs": A(), "milestones": A(), "deliverables": A(), "outlay": A(),
    "total_outlay": Nn(), "currency": Sn(),
    "submitted_by": Sn(), "submitted_at": Sn(),
    "decision": Sn(), "approved_at": Sn(), "reason": Sn(),
    "supersedes": Nn(), "tenant": Sn(), "created_at": Sn(),
    # --- ADR-0071 funding-pivot (all optional) -------------------------------------------------------
    "answers": An(),        # [governance.recommendation | welfare.priority ids] — the diagnosis it responds to
    "evidence": An(),       # [signal.report ids | document refs] — citizen/JE ground-truth
    "site": On(),           # { jurisdiction, cells:[h3], subject_kind }
    "intervention": On(),   # { kind, description, unit_cost_cr, env_resource, env_draw, from_catalog }
    "ask": On(),            # { amount_cr, currency, mode: addressed|open|self, program, beneficiaries }
  },
}

def run():
    steward, serr = _post(IDN + "/realms/airawat/login", {"username": "steward"})
    if not steward: raise SystemExit(f"steward login failed: {serr}")
    stoken = steward["access_token"]
    cj = http.cookiejar.CookieJar()
    _post(APP + "/login", {"realm": "airawatdeveloper", "username": "devrick"}, cj=cj)
    # lint first
    adv, aerr = _post(NET + "/v1/schemas/advise", {"schema_id": "project.proposal", "definition": DEFN, "x_kind": "record"})
    if adv is not None:
        print("advise violations:", adv.get("house_rule_violations"))
    pr, perr = _post(APP + "/bridge/schema-request",
                     {"schema_id": "project.proposal", "version": "0.2.0", "definition": DEFN, "x_kind": "record",
                      "justification": "ADR-0071 S1 — add optional funding-pivot fields (answers/evidence/site/intervention/ask)."},
                     cj=cj)
    if perr: raise SystemExit(f"PROPOSE ERR {perr}")
    dr, derr = _post(NET + "/v1/schema-requests/decide",
                     {"schema_id": "project.proposal", "version": "0.2.0", "decision": "approve"}, token=stoken)
    print("project.proposal@0.2.0:", "propose=" + str(pr.get("status")),
          "approve=" + ("OK " + dr["published_schema"] if dr else "ERR " + str(derr)))

if __name__ == "__main__":
    run()
