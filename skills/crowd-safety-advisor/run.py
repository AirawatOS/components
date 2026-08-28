#!/usr/bin/env python3
"""Crowd-Safety Advisor — a confined SKILL (INT-0002): Facts × Rules → a cited governance.recommendation.

Reads the current crush state (cell.crush_risk), finds the critical moment/site, and emits a CITED, GRADED
governance.recommendation per the ADR-0029/0046 decision spine: the AI RECOMMENDS, a human incident
commander later ADMITS. Rule-based + deterministic (auditable). It cites the governed crowd-crush density-
bands rule and the specific density-cell facts as evidence. All I/O is through the broker.
"""
import json, os, urllib.request
from collections import defaultdict

API   = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]

def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())

def context():
    req = urllib.request.Request(API + "/v1/context", headers={"X-Run-Token": TOKEN})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

def actions_for(band, site):
    ghat = "Ramkund" if "Ramkund" in (site or "") else "Kushavarta"
    if band == "critical":
        return [f"HALT inflow at all {ghat} approach gates immediately; hold pilgrims upstream",
                f"OPEN the emergency relief exit to decompress the {ghat} ghat",
                "PRE-POSITION 108 ambulances + medical teams at the ghat edge",
                "SURGE ground marshals to the pinch-point; enforce one-way flow",
                "PAUSE further Snan batches until density falls below 4/m²"]
    if band == "danger":
        return [f"DIVERT inflow away from {ghat}; meter entry at the approach gates",
                f"HOLD / stagger buses on the {ghat} routes to slow arrivals",
                "PRE-POSITION medical + marshals at the ghat; ready the relief exit",
                "BROADCAST an advisory to spread arrivals across bathing windows"]
    return [f"MONITOR {ghat}; pre-stage marshals; no restriction yet"]

def main():
    T = context().get("tenant") or os.environ.get("WRITE_TENANT", "kmc")
    rule_id = f"ds:{T}:governance/rule/crowd-crush-density-bands"
    crush = [r.get("object") or r for r in call("/v1/query", {"scope": "cell.crush_risk", "limit": 50000}).get("results", [])]
    if not crush:
        print("crowd-safety-advisor: no crush-risk cells — nothing to recommend (crowd below danger)."); return 0
    # find the critical MOMENT (frame with the highest density) and its site
    by = defaultdict(lambda: {"max": 0.0, "cells": [], "site": ""})
    for c in crush:
        k = (c.get("observed_at"), c.get("site", "?"))
        by[k]["max"] = max(by[k]["max"], c.get("density_sqm", 0) or 0)
        by[k]["cells"].append(c["id"]); by[k]["site"] = c.get("site", "?")
    (frame, site), st = max(by.items(), key=lambda kv: kv[1]["max"])
    dmax = st["max"]; band = "critical" if dmax >= 6 else "danger"
    n_cells = len(st["cells"]); scen = (crush[0].get("prov") or {}).get("scenario", "?")
    acts = actions_for(band, site)
    rid = f"ds:{T}:governance/recommendation/crowd-{(site or 'site').split()[0].lower()}-{(frame or '')[:13]}"
    rec = {"schema": "governance.recommendation", "id": rid, "tenant": T,
           "produced_by": f"component://airawatdeveloper/skill/crowd-safety-advisor@0.1.0",
           "skill": "ds:airawatdeveloper:skill/crowd-safety-advisor", "method": "rule",
           "confidence": 0.9, "created_at": frame, "score": 1,
           "title": f"{band.upper()} crowd risk at {site} — {dmax:.1f} people/m²",
           "summary": (f"At {(frame or '')[11:16]} the {site} ghat reached a simulated peak density of {dmax:.1f} "
                       f"people/m² across {n_cells} cells — into the {band.upper()} band "
                       f"(>{'6' if band=='critical' else '4.5'}/m²). This is the documented Ramkund pinch-point "
                       f"pattern. Recommended graded intervention below. [SIMULATION · scenario={scen}]"),
           "proposed_action": " · ".join(f"{i+1}) {x}" for i, x in enumerate(acts)),
           "rationale": f"Density {dmax:.1f}/m² exceeds the {band} threshold in the crowd-crush SOP.",
           "citation": {"rule": rule_id, "threshold": 6 if band == "critical" else 4.5},
           "for_facts": {"site": site, "frame": frame, "peak_density_sqm": dmax, "cells_in_band": n_cells},
           "evidence": st["cells"][:20], "narrative_templated": True}
    call("/v1/write", {"scope": "governance.recommendation", "id": rid, "object": rec})
    print(f"crowd-safety-advisor → {rec['title']} (frame={frame} band={band} cells={n_cells}) → {T}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
