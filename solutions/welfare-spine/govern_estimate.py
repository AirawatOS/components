#!/usr/bin/env python3
"""ADR-0070 — govern the `stat` domain + the `stat.estimate` aggregate measure (the publishability unit).
A published derived number is a stat.estimate fact: value + credible interval + coverage + method mix +
calibration version, subject = the jurisdiction/cell it summarises, derived_from = the record set.

Propose as devrick (app_host bridge) -> approve as steward (network). Local default; env-override for EC2:
AIROS_APP / AIROS_NET / AIROS_IDN. Run with no --publish to only ADVISE (house-rule check, no governance)."""
import os, sys, json, urllib.request, urllib.error, http.cookiejar

APP = os.environ.get("AIROS_APP", "http://localhost:8099")
NET = os.environ.get("AIROS_NET", "http://localhost:8090")
IDN = os.environ.get("AIROS_IDN", "http://localhost:8093")
ID_PAT = r"^ds:[a-z0-9-]+:\S+$"

def _post(url, body, token=None, cj=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    if token: req.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(opener.open(req, timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:220]}"

S = lambda: {"type": "string"}
Sn = lambda: {"type": ["string", "null"]}
On = lambda: {"type": ["object", "null"]}
An = lambda: {"type": ["array", "null"]}
Nn = lambda: {"type": ["number", "null"]}
In = lambda: {"type": ["integer", "null"]}
Bn = lambda: {"type": ["boolean", "null"]}

DESC = ("The ESTIMATE — the unit of any PUBLISHED derived aggregate (ADR-0070). subject = the jurisdiction/cell "
        "it summarises; derived_from = the record set. Carries the credible interval + coverage (observed/target) "
        "+ method mix + calibration version, so 'no unqualified derived number' is structural: an aggregate cannot "
        "be a bare value. `metric` names what is estimated (e.g. welfare.gap.below_floor_fraction) — reusable "
        "beyond welfare (tax-gap rates, coverage QA, air-quality ward means). `publishable` is set by the governed "
        "publishability gate (min coverage, max interval width).")

ESTIMATE = {
  "type": "object", "x-kind": "measure", "additionalProperties": False,
  "required": ["schema", "id", "subject", "observed_at", "prov", "metric", "value", "coverage"],
  "title": "stat.estimate", "description": DESC,
  "properties": {
    "schema": {"const": "stat.estimate"}, "id": {"type": "string", "pattern": ID_PAT},
    "subject": S(), "subject_kind": {"enum": ["jurisdiction", "cell", "ward", "city", "household", "building"]},
    "observed_at": S(), "prov": {"type": "object"}, "derived_from": An(),
    "method": {"enum": ["analytic", "montecarlo", "measured", "modelled", "estimated"]},
    "metric": S(),                                          # what is estimated (dotted metric name)
    "capability": Sn(),                                     # welfare.capability id, when applicable
    "floor_ref": Sn(),                                      # welfare.floor id, when applicable
    "value": {"type": "number"}, "unit": Sn(),
    "ci_low": Nn(), "ci_high": Nn(), "ci_level": Nn(),      # credible interval + level (e.g. 0.9)
    "coverage": {"type": "number"},                        # observed/target (0..1) — the dominant honesty term
    "n_observed": In(), "n_target": In(),
    "method_mix": On(),                                    # {metered, modelled, imputed, ...} fractions
    "model_version": Sn(),                                 # calibration in force
    "publishable": Bn(),                                  # gate outcome; false => provisional, never a headline
    "confidence": Nn(), "tenant": Sn()},
}

def advise():
    r, e = _post(NET + "/v1/schemas/advise", {"schema_id": "stat.estimate", "definition": ESTIMATE, "kind": "schema"})
    if e: print("advise ERR", e); return False
    v = r.get("house_rule_violations") or []
    print("house_rule_violations:", v or "none")
    print("recommendation:", r.get("recommendation"))
    return not v

def govern():
    steward, serr = _post(IDN + "/realms/airawat/login", {"username": "steward"})
    if not steward: raise SystemExit(f"steward login failed: {serr}")
    stoken = steward["access_token"]
    cj = http.cookiejar.CookieJar()
    _post(APP + "/login", {"realm": "airawatdeveloper", "username": "devrick"}, cj=cj)
    r, e = _post(NET + "/v1/domains", {"domain": "stat", "label": "Statistics"}, token=stoken)
    print("domain stat:", ("OK " + r["published"]["id"]) if r else ("(exists/err) " + str(e)))
    pr, perr = _post(APP + "/bridge/schema-request",
                     {"schema_id": "stat.estimate", "version": "0.1.0", "definition": ESTIMATE,
                      "x_kind": "measure", "justification": "ADR-0070 — the publishability unit for derived aggregates."}, cj=cj)
    if perr: print("propose ERR", perr); return
    dr, derr = _post(NET + "/v1/schema-requests/decide", {"schema_id": "stat.estimate", "version": "0.1.0", "decision": "approve"}, token=stoken)
    print("stat.estimate:", f"propose={pr.get('status')} approve={'OK '+dr['published_schema'] if dr else 'ERR '+str(derr)}")

if __name__ == "__main__":
    if "--publish" in sys.argv: govern()
    else: advise()
