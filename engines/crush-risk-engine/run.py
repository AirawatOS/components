#!/usr/bin/env python3
"""Crush-Risk Engine — a confined engine. Reads crowd-density facts, applies the governed density-band
rule, and writes derived `cell.crush_risk` facts (L3). This is the DERIVATION step split out of the crowd
sensor: the sensor writes cell.crowd_density; this engine turns density into a graded crush-risk band per
the `crowd-crush-density-bands` rule (>=4.5/m² DANGER, >=6/m² CRITICAL). All I/O is through the broker.
"""
import json, os, urllib.request

API   = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]

def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())

def context():
    req = urllib.request.Request(API + "/v1/context", headers={"X-Run-Token": TOKEN})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

def band(d, danger, critical):
    return "critical" if d >= critical else "danger" if d >= danger else None

def main():
    T = context().get("tenant") or os.environ.get("WRITE_TENANT", "kmc")
    # thresholds come from the governed rule (fall back to the documented bands if it isn't seeded yet)
    danger, critical = 4.5, 6.0
    for r in (call("/v1/query", {"scope": "governance.rule", "limit": 500}).get("results") or []):
        o = r.get("object") or r
        if o.get("rule_id") == "crowd-crush-density-bands" and isinstance(o.get("value"), list) and len(o["value"]) >= 2:
            danger, critical = float(o["value"][0]), float(o["value"][1]); break

    rows = [r.get("object") or r for r in call("/v1/query", {"scope": "cell.crowd_density", "limit": 50000}).get("results", [])]
    written = 0
    for c in rows:
        d = c.get("density_sqm")
        if d is None:
            continue
        b = band(d, danger, critical)
        if not b:
            continue                                        # below the danger threshold → no crush-risk fact
        h3id = (c.get("space") or {}).get("h3_id") or c["id"].split("/")[-1]
        cid = f"ds:{T}:cell/crush_risk/{c['id'].split('/')[-1]}"
        obj = {"schema": "cell.crush_risk", "id": cid, "tenant": T, "layer": "L3",
               "space": c.get("space"), "observed_at": c.get("observed_at"), "density_sqm": d,
               "band": b, "threshold": danger, "site": c.get("site"),
               "method": "density-threshold", "derived_from": c["id"], "prov": c.get("prov")}
        call("/v1/write", {"scope": "cell.crush_risk", "id": cid, "object": obj})
        written += 1
    print(f"crush-risk-engine: read {len(rows)} density cells; wrote {written} cell.crush_risk "
          f"(danger>={danger}, critical>={critical}) to {T}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
