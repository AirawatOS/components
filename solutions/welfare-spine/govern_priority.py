#!/usr/bin/env python3
"""Welfare spine — govern `welfare.priority` (ADR-0069 cell-grain prioritisation).

A per-cell (per-unit) prioritisation record: the worst-off-first RANK of a unit for one capability, its
prioritarian score, the persons it represents, and — under a stated budget — whether it is FUNDED or DEFERRED
and which constraint bound it. This is the map-able bridge from the city-aggregate recommendation to
'what to do WHERE': every cell knows its rank and its fundability. Analysis (not a committed decision) — a
human still admits a governance.decision.

Propose as devrick (app_host /bridge/schema-request); approve as steward (network decide). Same house-rule-clean
measure shape as govern_schemas.py. LOCAL by default; override AIROS_APP/AIROS_NET/AIROS_IDN for EC2."""
import os, json, urllib.request, urllib.error, http.cookiejar

APP = os.environ.get("AIROS_APP", "http://localhost:8099")
NET = os.environ.get("AIROS_NET", "http://localhost:8090")
IDN = os.environ.get("AIROS_IDN", "http://localhost:8093")
ID_PAT = r"^ds:[a-z0-9-]+:\S+$"

def _post(url, body, token=None, cj=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    if token: req.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try:
        return json.loads(opener.open(req, timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code} {e.read().decode()[:200]}"

S = lambda: {"type": "string"}
Sn = lambda: {"type": ["string", "null"]}
On = lambda: {"type": ["object", "null"]}
Nn = lambda: {"type": ["number", "null"]}

DESC = ("The worst-off-first RANK of one unit (cell/household) for one capability: its prioritarian score "
        "(persons x depth x deprivation), the persons it represents, and — under a stated fiscal + environmental "
        "budget — whether it is FUNDED or DEFERRED and which constraint bound it. The map-able bridge from the "
        "city-aggregate recommendation to per-cell action; recomputed each prioritisation run. Analysis, not a "
        "committed decision.")

DEFN = {
  "type": "object", "x-kind": "measure", "additionalProperties": False,
  "title": "welfare.priority", "description": DESC,
  "required": ["schema", "id", "subject", "observed_at", "prov", "capability", "rank", "score", "funded"],
  "properties": {
    "schema": {"const": "welfare.priority"}, "id": {"type": "string", "pattern": ID_PAT},
    "subject": S(),                                        # the ranked unit (civic.household / cell id)
    "subject_kind": {"enum": ["household", "cell", "building", "parcel", "jurisdiction"]},
    "observed_at": S(), "confidence": Nn(), "derived_from": {"type": ["array", "null"]},
    "prov": {"type": "object"},
    "method": {"enum": ["measured", "metered", "modelled", "interpolated", "surveyed", "declared", "derived"]},
    "tenant": Sn(),
    "capability": S(),                                     # welfare.capability id
    "space": On(),                                         # canonical H3 space block (ADR-0053) — for the map
    "rank": {"type": "number"},                            # 1 = worst-off (highest score) citywide
    "score": {"type": "number"},                           # persons_below x (1+depth) x (1+deprivation)
    "persons": Nn(),                                       # people this unit represents below the floor
    "depth": Nn(),                                         # shortfall / floor threshold
    "deprivation": Nn(),                                   # 0..1 prioritarian weight of this unit
    "shortfall": Nn(), "floor_value": Nn(), "level_value": Nn(),
    "cost_cr": Nn(),                                       # INR crore to lift this unit above the floor
    "env_resource": Sn(), "env_draw": Nn(),                # carrying-capacity resource + amount drawn
    "funded": {"type": "boolean"},                         # selected within budget this run
    "budget_binding": Sn(),                                # null if funded; else 'budget-fiscal'|'budget-env:<res>'
    "cumulative_cost_cr": Nn(), "cumulative_env_draw": Nn(),
    "gap_ref": Sn(),                                       # welfare.gap id this ranks
    "recommendation_ref": Sn(),                            # governance.recommendation the run emitted
  },
}

def run():
    steward, serr = _post(IDN + "/realms/airawat/login", {"username": "steward"})
    if not steward: raise SystemExit(f"steward login failed: {serr}")
    stoken = steward["access_token"]
    cj = http.cookiejar.CookieJar()
    _post(APP + "/login", {"realm": "airawatdeveloper", "username": "devrick"}, cj=cj)
    pr, perr = _post(APP + "/bridge/schema-request",
                     {"schema_id": "welfare.priority", "version": "0.1.0", "definition": DEFN,
                      "x_kind": "measure",
                      "justification": "Welfare spine cell-grain prioritisation (ADR-0069) — per-cell rank + fundability."},
                     cj=cj)
    if perr: raise SystemExit(f"PROPOSE ERR {perr}")
    dr, derr = _post(NET + "/v1/schema-requests/decide",
                     {"schema_id": "welfare.priority", "version": "0.1.0", "decision": "approve"}, token=stoken)
    print(f"welfare.priority: propose={pr.get('status')} approve={'OK '+dr['published_schema'] if dr else 'ERR '+str(derr)}")

if __name__ == "__main__":
    run()
