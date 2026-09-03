#!/usr/bin/env python3
"""Welfare spine Slice 1 (ADR-0069) — govern `welfare` + `infra` domains (extend `civic`) and
propose/approve the seven capability-agnostic nouns of the objective function.

  welfare.capability  · welfare.floor  · civic.household · infra.service_connection
  welfare.service_level · welfare.deprivation · welfare.gap (derived)

Propose as devrick (app_host /bridge/schema-request); approve as steward (network /v1/schema-requests/decide).
Same house-rule-clean shapes as the Org Foundation govern scripts. Targets the LOCAL stack by default
(override with AIROS_APP/AIROS_NET/AIROS_IDN env vars to point at EC2)."""
import os, json, urllib.request, urllib.error, http.cookiejar

APP = os.environ.get("AIROS_APP", "http://localhost:8099")   # app_host (Studio bridges)
NET = os.environ.get("AIROS_NET", "http://localhost:8090")   # network directory (governance)
IDN = os.environ.get("AIROS_IDN", "http://localhost:8093")   # identity

ID_PAT = r"^ds:[a-z0-9-]+:\S+$"

def _post(url, body, token=None, cj=None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    if token: req.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try:
        return json.loads(opener.open(req, timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code} {e.read().decode()[:200]}"

# --- type helpers ----------------------------------------------------------
S  = lambda: {"type": "string"}
Sn = lambda: {"type": ["string", "null"]}
On = lambda: {"type": ["object", "null"]}
An = lambda: {"type": ["array", "null"]}
Nn = lambda: {"type": ["number", "null"]}
Bn = lambda: {"type": ["boolean", "null"]}

def ent(schema_id, required, props):
    """House-rule-clean ENTITY schema (R1 object, R2 schema+id, R2b id pattern, R3 closed, R4 typed)."""
    p = {"schema": {"const": schema_id}, "id": {"type": "string", "pattern": ID_PAT}}
    p.update(props)
    return {"type": "object", "x-kind": "entity", "additionalProperties": False,
            "required": ["schema", "id"] + required, "properties": p,
            "title": schema_id, "description": DESC[schema_id]}

def meas(schema_id, required, props):
    """House-rule-clean MEASURE schema — a value about a subject at a time, with provenance (ADR-0041).
    subject/subject_kind + observed_at + prov are the measure backbone; confidence + derived_from carry lineage."""
    p = {"schema": {"const": schema_id}, "id": {"type": "string", "pattern": ID_PAT},
         "subject": S(), "subject_kind": {"enum": ["household", "cell", "building", "parcel", "jurisdiction"]},
         "observed_at": S(), "confidence": Nn(), "derived_from": An(), "prov": {"type": "object"},
         "method": {"enum": ["measured", "metered", "modelled", "interpolated", "surveyed", "declared", "derived"]},
         "tenant": Sn()}
    p.update(props)
    return {"type": "object", "x-kind": "measure", "additionalProperties": False,
            "required": ["schema", "id", "subject", "observed_at", "prov"] + required, "properties": p,
            "title": schema_id, "description": DESC[schema_id]}

DESC = {
 "welfare.capability":
   "A basic capability the city exists to raise (housing, water, sanitation, energy, mobility, livelihood, "
   "health, education, safety, environment, voice) — the objective-function vector, governed as vocabulary. "
   "service_unit is how delivery is measured (LPCD, hours/day, minutes-to-facility); floor_direction says "
   "whether more or less is better.",
 "welfare.floor":
   "The dignity floor for a capability: the threshold a person must reach, keyed on settlement class / "
   "population / terrain, with its legal force (statutory | SLB | recommended | contractual) and cited source. "
   "The URDPFI/SLB obligation, welfare-anchored. Change the floor and every gap recomputes.",
 "civic.household":
   "The equity unit: a dwelling-occupancy unit at a location (building/parcel/cell), with size and tenure — "
   "NON-PII. It is what service levels and deprivation attach to, so 'weight the worst-served' is computable "
   "without a surveillance graph. Persons (PII) live in civic.resident, joined only under authorisation.",
 "infra.service_connection":
   "The join between a service network and a premise: does this household/building receive a given capability "
   "(water tap, sewer, power, ...). Capability-agnostic. status=illegal is a first-class value, so exclusion "
   "and theft are the same query. Carries the external consumer id for entity-resolution.",
 "welfare.service_level":
   "What is actually delivered to a unit for one capability over an interval (e.g. 51 LPCD to household H over "
   "March). The measured input to the floor comparison; method + confidence + prov make it auditable.",
 "welfare.deprivation":
   "How badly-off a unit is across the capability vector — the prioritarian weight (0..1 composite, MPI-style), "
   "with the per-capability shortfalls it was built from. Derived; recomputed when service levels change.",
 "welfare.gap":
   "DERIVED, never primary: floor - service_level for a unit and capability, times the deprivation weight, with "
   "the evidence (floor + level + deprivation) in derived_from. The gap-engine's output; the trigger a "
   "recommendation is built from.",
}

SCHEMAS = {
 "welfare.capability": ent("welfare.capability", ["label", "service_unit", "floor_direction"], {
    "label": S(), "service_unit": S(),                       # e.g. "LPCD", "hours/day", "minutes_to_facility"
    "floor_direction": {"enum": ["higher_is_better", "lower_is_better"]},
    "category": Sn(),                                        # optional grouping (basic-services | opportunity | protection ...)
    "description": Sn(), "tenant": Sn()}),

 "welfare.floor": ent("welfare.floor", ["capability", "threshold", "unit", "legal_force"], {
    "capability": S(),                                       # welfare.capability id
    "threshold": {"type": "number"}, "unit": S(),
    "bound": {"enum": ["min", "max"]},                       # a floor is usually a min; a max for lower_is_better capabilities
    "applies_when": On(),                                    # { settlement_class:[], terrain, pop_min, pop_max }
    "legal_force": {"enum": ["statutory", "slb", "recommended", "contractual"]},
    "source": Sn(),                                          # ref.source id (document + citation)
    "effective_from": Sn(), "note": Sn(), "tenant": Sn()}),

 "civic.household": ent("civic.household", [], {
    "building_ref": Sn(),                                    # geo.building id
    "parcel_ref": Sn(),                                      # geo.parcel id
    "space": On(),                                           # canonical H3 space block (ADR-0053)
    "size": Nn(),                                            # persons (may be estimated -> confidence)
    "tenure": {"enum": ["owned", "rented", "informal", "homeless", "institutional", "unknown", None]},
    "dwelling_type": Sn(),
    "connections": An(),                                     # infra.service_connection ids
    "survey_source": Sn(),                                   # ref.source id (census/slum-survey/estimate)
    "confidence": Nn(),                                      # quality of this household assertion
    "supersedes": An(),                                      # prior civic.household ids (subdivision/merge lineage)
    "tenant": Sn(), "created_at": Sn()}),

 "infra.service_connection": ent("infra.service_connection", ["capability", "serves"], {
    "capability": S(),                                       # welfare.capability id
    "serves": S(),                                           # civic.household | geo.building | geo.parcel id
    "serves_kind": {"enum": ["household", "building", "parcel"]},
    "provider": Sn(),                                        # operating agent / network id
    "status": {"enum": ["active", "none", "illegal", "applied", "disconnected", "abandoned"]},
    "metered": Bn(),
    "consumer_ref": Sn(),                                    # external consumer/connection id -> entity-resolution
    "sanctioned": On(),                                      # { size | load | ... }
    "tariff_category": Sn(),
    "network_ref": Sn(),                                     # infra.network id (later slice)
    "tenant": Sn(), "created_at": Sn()}),

 "welfare.service_level": meas("welfare.service_level", ["capability", "value"], {
    "capability": S(),                                       # welfare.capability id
    "value": {"type": "number"}, "unit": Sn(),
    "interval": On(),                                        # { start, end } when the level is a rate over a period
    "source_ref": Sn()}),                                    # ref.source id

 "welfare.deprivation": meas("welfare.deprivation", ["index"], {
    "index": {"type": "number"},                            # 0..1 composite (higher = more deprived)
    "shortfalls": On(),                                      # { capability_id: normalised_shortfall }
    "deprived": An(),                                        # capability ids below floor
    "headcount_weight": Nn()}),                              # persons this unit represents (for aggregation)

 "welfare.gap": meas("welfare.gap", ["capability", "shortfall"], {
    "capability": S(),                                       # welfare.capability id
    "floor_ref": Sn(),                                       # welfare.floor id invoked
    "floor_value": Nn(), "level_value": Nn(),
    "shortfall": {"type": "number"},                        # floor - level (>=0 means below floor)
    "weight": Nn(),                                          # deprivation weight applied
    "weighted": Nn()}),                                     # shortfall x weight — the prioritisation key
}

DOMAINS = [("welfare", "Welfare"), ("infra", "Infrastructure"), ("civic", "Civic")]
ORDER = ["welfare.capability", "welfare.floor", "civic.household", "infra.service_connection",
         "welfare.service_level", "welfare.deprivation", "welfare.gap"]

def run():
    # --- creds -------------------------------------------------------------
    steward, serr = _post(IDN + "/realms/airawat/login", {"username": "steward"})
    if not steward: raise SystemExit(f"steward login failed: {serr}")
    stoken = steward["access_token"]
    cj = http.cookiejar.CookieJar()
    _post(APP + "/login", {"realm": "airawatdeveloper", "username": "devrick"}, cj=cj)   # devrick session cookie
    # --- 1) govern the domains (idempotent; civic may already exist) -------
    for dom, label in DOMAINS:
        r, err = _post(NET + "/v1/domains", {"domain": dom, "label": label}, token=stoken)
        print(f"domain {dom}:", ("OK " + r["published"]["id"]) if r else ("(exists/err) " + str(err)))
    # --- 2) propose (devrick) -> approve (steward) each schema -------------
    for sid in ORDER:
        defn = SCHEMAS[sid]
        pr, perr = _post(APP + "/bridge/schema-request",
                         {"schema_id": sid, "version": "0.1.0", "definition": defn,
                          "x_kind": defn["x-kind"], "justification": "Welfare spine slice 1 (ADR-0069) — objective-function backbone."},
                         cj=cj)
        if perr:
            print(f"  {sid}: PROPOSE ERR {perr}"); continue
        dr, derr = _post(NET + "/v1/schema-requests/decide",
                         {"schema_id": sid, "version": "0.1.0", "decision": "approve"}, token=stoken)
        print(f"  {sid}: propose={pr.get('status')} approve={'OK '+dr['published_schema'] if dr else 'ERR '+str(derr)}")

if __name__ == "__main__":
    run()
