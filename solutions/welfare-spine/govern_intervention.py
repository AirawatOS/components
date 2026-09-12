#!/usr/bin/env python3
"""ADR-0069 Step 2 — the INTERVENTION CATALOGUE. Govern `welfare.intervention`: the governed menu of LEVERS per
capability — the ways you can actually move a unit's service level toward its floor. Each lever carries a cost
and an environmental draw PER PERSON (so it slots straight into the per-cell prioritisation knapsack), plus a
feasibility hint and an optional funding scheme. This is what turns 'what's wrong here' into 'do X here' — and
why crowding relief that assumed the land-hungry lever funds only 3 cells, while a vertical lever funds far more.

Govern the schema (devrick→steward) + seed indicative crowding levers into kmc (kanpur-admin). LOCAL by default;
override AIROS_APP/NET/IDN + KMC_* for EC2."""
import os, json, urllib.request, urllib.error, http.cookiejar

APP = os.environ.get("AIROS_APP", "http://localhost:8099")
NET = os.environ.get("AIROS_NET", "http://localhost:8090")
IDN = os.environ.get("AIROS_IDN", "http://localhost:8093")
KER = os.environ.get("AIROS_KER", "http://localhost:8080")
ID_PAT = r"^ds:[a-z0-9-]+:\S+$"

def _post(url, body, token=None, cj=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    if token: req.add_header("Authorization", "Bearer " + token)
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(op.open(req, timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:220]}"

S = lambda: {"type": "string"}
Sn = lambda: {"type": ["string", "null"]}
On = lambda: {"type": ["object", "null"]}
Nn = lambda: {"type": ["number", "null"]}

DEFN = {
  "type": "object", "x-kind": "entity", "additionalProperties": False,
  "title": "welfare.intervention",
  "description": ("A LEVER in the intervention catalogue (ADR-0069 Step 2): a way to move a unit's service level "
                  "toward the floor for one capability, with its cost and environmental draw PER PERSON, a "
                  "feasibility hint, and an optional funding scheme. The prioritisation engine picks the "
                  "least-draw feasible lever per cell; the proposal drafter fills its intervention from it. "
                  "Costs/draws are governed config — INDICATIVE until replaced with cited DPR/scheme rates."),
  "required": ["schema", "id", "capability", "kind", "label"],
  "properties": {
    "schema": {"const": "welfare.intervention"}, "id": {"type": "string", "pattern": ID_PAT},
    "capability": S(),                                    # welfare.capability id
    "kind": S(),                                          # short slug, e.g. "vertical-redevelopment"
    "label": S(),                                         # human name, e.g. "In-situ vertical redevelopment"
    "description": Sn(),
    "unit_cost_cr_per_person": Nn(),                      # INR crore to lift one person above the floor via this lever
    "env_resource": Sn(),                                 # land | aquifer | airshed | carbon | energy | waste | null
    "env_draw_per_person": Nn(),                          # units of that resource drawn per person (e.g. ha/person)
    "effect": On(),                                       # { moves: "level_down"|"level_up"|"raises_floor", note }
    "applies_when": On(),                                 # feasibility hint { min_density, tenure, land_available, ... }
    "from_scheme": Sn(),                                  # a funding scheme this lever typically draws on (SBM/PMAY/AMRUT…)
    "basis": Sn(), "confidence": Nn(), "tenant": Sn(), "created_at": Sn(),
  },
}

# --- indicative crowding levers (the point: they differ ENORMOUSLY in land draw) ------------------------------
def _levers(tenant):
    cap = f"ds:{tenant}:welfare/capability/residential_density"
    L = lambda k, label, cost, res, draw, moves, scheme, note, feas: {
        "schema": "welfare.intervention", "id": f"ds:{tenant}:welfare/intervention/{k}", "tenant": tenant,
        "capability": cap, "kind": k, "label": label, "description": note,
        "unit_cost_cr_per_person": cost, "env_resource": res, "env_draw_per_person": draw,
        "effect": {"moves": moves}, "applies_when": feas, "from_scheme": scheme,
        "basis": "INDICATIVE — replace with cited DPR / scheme rates", "confidence": 0.4}
    return [
      L("vertical-redevelopment", "In-situ vertical redevelopment (raise FSI)", 0.008, "land", 0.0002, "level_down",
        "PMAY-U (ISSR/AHP)", "Rebuild taller on the same footprint — relieves crowding with little new land; needs tenure + consent.",
        {"min_density": 250, "tenure": ["owned", "informal"]}),
      L("infra-augmentation", "Infrastructure augmentation", 0.004, "land", 0.0003, "raises_floor",
        "AMRUT", "Add water/sewer/road capacity so a higher density is liveable — raises the tolerable ceiling rather than moving people.",
        {"min_density": 200}),
      L("relocation-supply", "Relocation + new housing supply", 0.006, "land", 0.0012, "level_down",
        "PMAY-U", "Move some households to new stock elsewhere — effective but land-hungry; needs land nearby.",
        {"land_available": True}),
    ]

def run():
    steward, serr = _post(IDN + "/realms/airawat/login", {"username": "steward"})
    if not steward: raise SystemExit(f"steward login: {serr}")
    stoken = steward["access_token"]
    cj = http.cookiejar.CookieJar()
    _post(APP + "/login", {"realm": "airawatdeveloper", "username": "devrick"}, cj=cj)
    adv, _ = _post(NET + "/v1/schemas/advise", {"schema_id": "welfare.intervention", "definition": DEFN, "x_kind": "entity"})
    print("advise violations:", (adv or {}).get("house_rule_violations"))
    pr, perr = _post(APP + "/bridge/schema-request",
                     {"schema_id": "welfare.intervention", "version": "0.1.0", "definition": DEFN, "x_kind": "entity",
                      "justification": "ADR-0069 Step 2 — intervention catalogue (levers per capability)."}, cj=cj)
    if perr: raise SystemExit(f"PROPOSE ERR {perr}")
    dr, derr = _post(NET + "/v1/schema-requests/decide",
                     {"schema_id": "welfare.intervention", "version": "0.1.0", "decision": "approve"}, token=stoken)
    print("welfare.intervention@0.1.0:", "OK " + dr["published_schema"] if dr else "ERR " + str(derr))
    # seed the crowding levers into kmc
    tenant = os.environ.get("SEED_TENANT", "kmc")
    ka, _ = _post(IDN + f"/realms/{tenant}/login", {"username": os.environ.get("SEED_USER", "kanpur-admin")})
    if not ka:
        print("(skipped seeding — no tenant admin token)"); return
    tok = ka["access_token"]
    for lev in _levers(tenant):
        r, e = _post(KER + "/v1/registry/objects", {"id": lev["id"], "object": lev}, token=tok)
        print("  seed", lev["kind"], "->", "OK" if r else e)

if __name__ == "__main__":
    run()
