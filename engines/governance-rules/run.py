#!/usr/bin/env python3
"""governance-rules engine: air.quality (facts) + governance.rule (rules) → governance.recommendation.

The RULES side of facts+rules→decision (ADR-0046). Confined — reads facts + rules and writes a
recommendation ONLY through the broker. DETERMINISTIC (no LLM): it evaluates each governance.rule whose
`applies_to` matches the fact schema against the AOI's fact surface, finds the highest-PRIORITY rule that
fires against the representative (mean) value, and emits ONE governance.recommendation carrying that rule's
proposed_action at its declared STRENGTH (mandatory/advisory), cited to the rule + its source instrument.
The whole rule cascade (fired/not, cells-over-threshold) is recorded as evidence — the match is auditable.
AI never decides — it recommends; an authority admits it into a governance.decision downstream.
"""
import json, os, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/kanpur-core")
FACT_SCHEMA = os.environ.get("FACT_SCHEMA", "air.quality")
METRIC = os.environ.get("METRIC", "pm25")
WRITE_TENANT = os.environ.get("WRITE_TENANT", "kmc")
CID = "ds:airawat:engine/governance-rules"

_OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b}


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _match(v, op, val):
    if op == "between":
        try:
            return val[0] <= v <= val[1]
        except Exception:
            return False
    f = _OPS.get(op)
    try:
        return bool(f and f(v, val))
    except Exception:
        return False


def main():
    aoi = _call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object")
    if not aoi:
        print(f"FAIL: no AOI at {AOI_ID}"); return 1
    rules = [r.get("object") or r for r in _call("/v1/query", {"scope": "governance.rule", "limit": 1000}).get("results", [])]
    # only LIVE rules drive decisions — a `proposed` rule (e.g. freshly extracted from a document) awaits
    # human approval before it can fire (AI proposes, never enacts).
    rules = [r for r in rules if r.get("applies_to") == FACT_SCHEMA and r.get("metric") == METRIC
             and r.get("status") != "proposed"]
    if not rules:
        print(f"no governance.rule for {FACT_SCHEMA}.{METRIC} — nothing to evaluate."); return 0
    cells = [r.get("object") or r for r in _call("/v1/query", {"scope": FACT_SCHEMA, "limit": 5000}).get("results", [])]
    vals = [c.get(METRIC) for c in cells if isinstance(c.get(METRIC), (int, float))]
    if not vals:
        print(f"no {FACT_SCHEMA}.{METRIC} facts to evaluate. Run the driver + interpolation first."); return 0

    n = len(vals); mean = round(sum(vals) / n, 1); mx = round(max(vals), 1); mn = round(min(vals), 1)
    evald = [{**r, "fires": _match(mean, r["operator"], r["value"]),
              "cells_over": sum(1 for v in vals if _match(v, r["operator"], r["value"]))} for r in rules]
    fired = [r for r in evald if r["fires"]]
    governing = max(fired, key=lambda r: r.get("priority", 0)) if fired else None
    obs = cells[0].get("observed_at", "")
    base = aoi.get("name", "AOI")

    if governing:
        title = f"{base}: {governing['stage']} air quality — {governing['binding']} action"
        summary = (f"PM2.5 across {base} (mean {mean}, max {mx} µg/m³ over {n} cells) crosses the "
                   f"'{governing['stage']}' threshold (pm25 {governing['operator']} {governing['value']}); "
                   f"{governing.get('authority')} · {governing['binding']}.")
        action = governing["proposed_action"]; strength = governing["binding"]
        cite = {"rule": governing["id"], "authority": governing.get("authority"), "source": governing.get("source")}
    else:
        title = f"{base}: air quality within thresholds"
        summary = f"PM2.5 across {base} (mean {mean}, max {mx} µg/m³ over {n} cells) crosses no governance.rule threshold."
        action = "No action required."; strength = "informational"; cite = {}

    cascade = [{"rule": r["rule_id"], "stage": r.get("stage"), "threshold": f"pm25 {r['operator']} {r['value']}",
                "binding": r["binding"], "fired": r["fires"], "cells_over": r["cells_over"]}
               for r in sorted(evald, key=lambda r: r.get("priority", 0))]

    rid = f"ds:{WRITE_TENANT}:governance/recommendation/air-quality-{obs[:13] or 'latest'}"
    rec = {"schema": "governance.recommendation", "id": rid, "title": title, "summary": summary,
           "proposed_action": action,
           "rationale": (f"Deterministic evaluation of {len(rules)} governance.rule against {n} {FACT_SCHEMA} "
                         f"cells; recommendation strength = {strength} (inherited from the governing instrument)."),
           "evidence": {"fact_schema": FACT_SCHEMA, "metric": METRIC, "cells": n, "mean": mean, "max": mx,
                        "min": mn, "observed_at": obs, "aoi": AOI_ID, "cascade": cascade},
           "confidence": 1.0, "score": governing.get("priority", 0) if governing else 0,
           "method": {"ref": {"id": CID, "version": "0.1.0"}, "kind": "deterministic-rules"},
           "produced_by": {"component": {"id": CID, "version": "0.1.0"}},
           "citation": cite, "created_at": obs, "tenant": WRITE_TENANT}
    _call("/v1/write", {"scope": "governance.recommendation", "id": rid, "object": rec,
                        "valid_time": {"from": obs}, "asserted_at": obs})

    print(f"recommendation: {title}")
    print(f"  {summary}")
    for c in cascade:
        print(f"   {'✓ FIRED ' if c['fired'] else '·       '}{c['stage']:9s} {c['threshold']:15s} [{c['binding']}] — {c['cells_over']}/{n} cells")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
