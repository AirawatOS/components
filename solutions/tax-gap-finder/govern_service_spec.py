"""ADR-0109 S1 — govern `platform.service_spec` + ingest the Property Tax spec as the FIRST instance.

The Service Specification (ADR-0109) is the single human-authored artifact for a civic service: it
REFERENCES governed pieces (schemas / interfaces / workflow / rules / roles) rather than duplicating them.
This script governs the schema (a light envelope with an open structured `spec` body + the authored
`source_yaml`) and writes the reverse-authored Property Tax spec as the first governed instance, so the
traceability index (noun/rule/acceptance → impl id, with status + test) is queryable in the registry.

Run against a reachable stack (defaults = the local airawat stack):
    python3 govern_service_spec.py
    APP=… NET=… IDN=… python3 govern_service_spec.py
"""
import json, os, pathlib, urllib.request, urllib.error, http.cookiejar

# NOTE: IDN, NET, APP and KERNEL must all belong to the SAME stack (consistent issuer/JWKS). A split
# dev setup (app-host wired to one network, identity to another) fails with "unknown signing key".
APP = os.environ.get("APP", "http://localhost:8099")
NET = os.environ.get("NET", "http://localhost:8090")
IDN = os.environ.get("IDN", "http://localhost:8093")
KERNEL = os.environ.get("KERNEL", "http://localhost:8080")
TENANT = os.environ.get("TENANT", "airawatdeveloper")
ID_PAT = r"^ds:[a-z0-9-]+:\S+$"
SID = "platform.service_spec"

def post(url, body, token=None, cj=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try:
        return json.loads(op.open(req, timeout=30).read() or b"{}"), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code} {e.read().decode()[:300]}"

# --- sessions: steward (approves governance) + devrick (authors in the dev tenant) ---
STOK = (post(IDN + "/realms/airawat/login", {"username": "steward"})[0] or {}).get("access_token")
cj = http.cookiejar.CookieJar()
post(APP + "/login", {"realm": TENANT, "username": "devrick"}, cj=cj)

# --- 1. govern platform.service_spec (light envelope; open `spec` body) ---
S = {"type": "string"}; Sn = {"type": ["string", "null"]}; O = {"type": "object"}
defn = {"type": "object", "x-kind": "record", "additionalProperties": False,
        "required": ["schema", "id", "capability"], "title": SID,
        "description": ("A SERVICE SPECIFICATION (ADR-0109): the single human-authored artifact for a civic "
                        "service. REFERENCES governed schemas/interfaces/workflow/rules/roles rather than "
                        "duplicating them. `spec` holds the structured, queryable body (each element carries an "
                        "`impl` id for traceability, or null where unbuilt); `source_yaml` holds the authored source."),
        "properties": {"schema": {"const": SID}, "id": {"type": "string", "pattern": ID_PAT},
                       "capability": S, "version": Sn, "realized_by": Sn, "status": Sn,
                       "spec": O, "source_yaml": Sn,
                       "tenant": Sn, "created_by": Sn, "created_at": Sn}}

pr, pe = post(APP + "/bridge/schema-request",
              {"schema_id": SID, "version": "0.1.0", "definition": defn, "x_kind": "record",
               "justification": "ADR-0109: the Service Specification as a first-class governed artifact for AI-native SDLC."},
              cj=cj)
print("propose platform.service_spec:", "OK" if not pe else pe)
dr, de = post(NET + "/v1/schema-requests/decide", {"schema_id": SID, "version": "0.1.0", "decision": "approve"}, token=STOK)
print("approve platform.service_spec:", (dr.get("published_schema") if dr else "ERR " + str(de)))

# --- 2. ingest the Property Tax spec as the first instance ---
yml = pathlib.Path(__file__).with_name("property-tax.service-spec.yaml").read_text()
iid = f"ds:{TENANT}:service_spec/property-tax"
spec_index = {
  "domain_model": [
    {"noun": "geo.parcel", "status": "built"}, {"noun": "tax.assessment", "status": "built"},
    {"noun": "util.connection", "status": "built"}, {"noun": "tax.change_signal", "status": "built"},
    {"noun": "tax.candidate", "status": "built"}, {"noun": "cell.tax_gap", "status": "built"},
    {"noun": "geo.building", "status": "built"},
    {"noun": "tax.demand", "status": "roadmap", "impl": None},
    {"noun": "tax.payment", "status": "roadmap", "impl": None},
    {"noun": "tax.objection", "status": "roadmap", "impl": None}],
  "operations": [
    {"verb": "tax.matchpolicy_get/set", "status": "built", "impl": "tax-gap-engine/run.py:192"},
    {"verb": "tax.match_preview", "status": "built", "impl": "tax-gap-engine/run.py:212"},
    {"verb": "tax.gap_scan", "status": "built", "impl": "tax-gap-engine/run.py:222"},
    {"verb": "campaign.create/assign/record_verification/progress", "status": "built", "impl": "campaign-engine/run.py"},
    {"verb": "tax.raise_demand", "status": "roadmap", "impl": None},
    {"verb": "tax.record_payment", "status": "roadmap", "impl": None}],
  "business_rules": [
    {"id": "PT-LINK-KEYJOIN", "status": "built", "impl": "tax-gap-engine/run.py gap-scan"},
    {"id": "PT-LINK-FUZZY", "status": "built", "impl": "tax-gap-engine/run.py:75"},
    {"id": "PT-LINK-SPATIAL", "status": "built", "impl": "tax-gap-engine/run.py:144"},
    {"id": "PT-GAP-UTILITY", "status": "built", "impl": "tax-gap-engine/run.py:260"},
    {"id": "PT-GAP-FOOTPRINT", "status": "built", "impl": "tax-gap-engine/run.py:270"},
    {"id": "PT-GAP-CHANGE", "status": "built", "impl": "tax-gap-engine/run.py:285"},
    {"id": "PT-CAND-CONF", "status": "built", "impl": "tax-gap-engine/run.py:300"},
    {"id": "PT-ASSESS-FORMULA", "status": "roadmap", "impl": None},
    {"id": "PT-DEMAND-DUEDATE", "status": "roadmap", "impl": None}],
  "workflow": {
    "candidate": {"states": ["flagged", "assigned", "verifying", "verified", "dismissed"], "status": "built"},
    "revenue": {"states": ["verified", "assessed", "demanded", "collected"], "status": "roadmap"}},
  "roles": [
    {"role": "platform.tenant-admin", "status": "built"}, {"role": "field-officer", "status": "built"},
    {"role": "assessor", "status": "roadmap"}, {"role": "citizen", "status": "roadmap"}],
  "acceptance_criteria": [
    {"id": "AC-GAP-FOOTPRINT", "status": "built", "test": None, "traces": ["PT-GAP-FOOTPRINT"]},
    {"id": "AC-LINK-REVIEW", "status": "built", "test": None, "traces": ["PT-LINK-FUZZY", "PT-GAP-UTILITY"]},
    {"id": "AC-CONF-FLOOR", "status": "built", "test": None, "traces": ["PT-CAND-CONF"]},
    {"id": "AC-VERIFY-CLOSE", "status": "built", "test": None},
    {"id": "AC-NO-FABRICATION", "status": "built", "test": None},
    {"id": "AC-DEMAND-ISSUE", "status": "roadmap", "test": None, "traces": ["PT-ASSESS-FORMULA", "PT-DEMAND-DUEDATE"]}],
  "nfr": {"documents_credentials": "roadmap", "fees": "roadmap", "slas": "roadmap", "events": "partial", "privacy": "roadmap"}}

rec = {"schema": SID, "id": iid,
       "capability": "Find, verify, assess, demand and collect property tax.",
       "version": "0.1.0", "realized_by": "ds:airawat:solution/tax-gap-finder", "status": "draft",
       "spec": spec_index, "source_yaml": yml}

# Attributed governed seed — NOT an app /bridge/save (which enforces an app's declared writes). This is the
# same steward attributed-write path the portal republish uses. The kernel strict-validates against the
# just-governed schema, so this requires the schema to have propagated to the kernel's catalog.
sr, se = post(KERNEL + "/v1/registry/objects", {"id": iid, "object": rec}, token=STOK)
print("ingest instance:", ("OK " + iid) if not se else ("ERR " + str(se)))

# --- 3. read-back: prove it's a governed, queryable registry object + a traceability summary ---
rr, re_ = post(KERNEL + "/v1/registry/resolve", {"id": iid}, token=STOK)
rows = [rr["object"]] if (rr and rr.get("object")) else []
print(f"read-back platform.service_spec -> {len(rows)} instance(s)" + (f"; id={rows[0].get('id')}" if rows else f" (raw: {str(rr)[:160]} {re_ or ''})"))
if rows:
    sp = (rows[0].get("spec") or {})
    built = sum(1 for g in [sp.get("domain_model", []), sp.get("operations", []), sp.get("business_rules", [])] for x in g if x.get("status") == "built")
    roadmap = sum(1 for g in [sp.get("domain_model", []), sp.get("operations", []), sp.get("business_rules", [])] for x in g if x.get("status") == "roadmap")
    no_test = [a["id"] for a in sp.get("acceptance_criteria", []) if a.get("test") is None]
    print(f"  traceability: {built} built elements, {roadmap} roadmap; acceptance criteria with NO test: {no_test}")
