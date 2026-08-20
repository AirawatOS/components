#!/usr/bin/env python3
"""flood-advisory SKILL (INT-0002): risk.flood Facts × flood Rules → cited governance.recommendation.

HYBRID reasoning: a DETERMINISTIC core evaluates the approved governance.rule for risk.flood against the
AOI's predicted flood risk and selects the highest-PRIORITY rule that fires (auditable, no model); the LLM
then writes the human-readable advisory grounded ONLY in that rule + the predicted risk. Flood is driven by
the WORST-affected cells, so the representative value is the MAX ponding-risk score (+ how many cells breach).
The recommendation's SUBSTANCE is rule-derived (method="rule"); the model only phrases it. Confined: broker
only. AI recommends; an authority decides downstream (decision-router → inbox). No synthetic data — no rule
fires ⇒ no recommendation; model unavailable ⇒ a templated rationale, LOUDLY flagged.
"""
import json, os, time, urllib.request, urllib.error

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/kanpur-core")
FACT_SCHEMA = os.environ.get("FACT_SCHEMA", "risk.flood")
METRIC = os.environ.get("METRIC", "score")
WRITE_TENANT = os.environ.get("WRITE_TENANT", "kmc")
REASONING = os.environ.get("REASONING", "hybrid")
CID = "ds:airawat:skill/flood-advisory"; VER = "0.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))

_OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b}


def _call(path, body, timeout=60):
    for a in range(4):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if e.code >= 500 and a < 3:
                time.sleep(1.5 * (a + 1)); continue
            raise
        except urllib.error.URLError:
            if a < 3:
                time.sleep(1.5 * (a + 1)); continue
            raise


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


def _narrative(base, rule, stats):
    try:
        instr = open(os.path.join(HERE, "SKILL.md")).read()
    except Exception:
        instr = ""
    prompt = (f"{instr}\n\n---\nArea: {base}\n"
              f"Governing rule that fired — band '{rule.get('stage')}', flood-risk score {rule['operator']} {rule['value']}, "
              f"authority {rule.get('authority')}, {rule.get('binding')}. Mandated action: {rule.get('proposed_action')}.\n"
              f"Predicted now — worst-cell ponding-risk score {stats['max']} (of 1.0); {stats['breached']} of {stats['n']} "
              f"cells at or above the threshold; rainfall forecast {stats['rain']}mm/24h.\n"
              f"Write the flood advisory now.")
    try:
        res = _call("/v1/complete", {"messages": [{"role": "user", "content": prompt}]}, timeout=60)
        txt = (res.get("content") or "").strip()
        if txt:
            return txt, res.get("model"), False
    except Exception:
        pass
    return (f"Predicted flood risk across {base}: worst-cell score {stats['max']}, {stats['breached']} of {stats['n']} "
            f"cells at/above the '{rule.get('stage')}' threshold (score {rule['operator']} {rule['value']}). "
            f"{rule.get('authority')} · {rule.get('binding')}: {rule.get('proposed_action')} "
            f"[advisory narrative model unavailable — templated]", None, True)


def main():
    aoi = _call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object")
    if not aoi:
        print(f"FAIL: no AOI at {AOI_ID}"); return 1
    base = aoi.get("name", "AOI")

    rules = [r.get("object") or r for r in _call("/v1/query", {"scope": "governance.rule", "limit": 1000}).get("results", [])]
    rules = [r for r in rules if r.get("applies_to") == FACT_SCHEMA and r.get("metric") == METRIC and r.get("status") != "proposed"]
    if not rules:
        print(f"no approved governance.rule for {FACT_SCHEMA}.{METRIC} — nothing to apply."); return 0

    cells = [c.get("object") or c for c in _call("/v1/query", {"scope": FACT_SCHEMA, "limit": 20000}).get("results", [])]
    vals = [c.get(METRIC) for c in cells if isinstance(c.get(METRIC), (int, float))]
    if not vals:
        print(f"no {FACT_SCHEMA}.{METRIC} facts — run the flood-risk engine first."); return 0
    n = len(vals); mx = round(max(vals), 3); mean = round(sum(vals) / n, 3)
    rain = ((cells[0].get("drivers") or {}).get("rainfall_mm"))
    obs = cells[0].get("observed_at", "")

    # flood is driven by the WORST cells → evaluate the MAX score against the rules
    evald = [{**r, "fires": _match(mx, r["operator"], r["value"]),
              "cells_over": sum(1 for v in vals if _match(v, r["operator"], r["value"]))} for r in rules]
    fired = [r for r in evald if r["fires"]]
    governing = max(fired, key=lambda r: r.get("priority", 0)) if fired else None
    if not governing:
        print(f"{base}: worst-cell flood-risk {mx} — no rule threshold crossed. No recommendation."); return 0
    stats = {"n": n, "max": mx, "mean": mean, "breached": governing["cells_over"], "rain": rain}

    if REASONING == "deterministic":
        narrative, model, fell_back = (f"Predicted flood risk across {base}: worst-cell score {mx}, "
                                       f"{governing['cells_over']} cells at/above '{governing['stage']}'. "
                                       f"{governing.get('authority')} · {governing.get('binding')}: "
                                       f"{governing.get('proposed_action')}", None, False)
    else:
        narrative, model, fell_back = _narrative(base, governing, stats)

    src = governing.get("source") or {}
    cascade = [{"rule": r.get("rule_id"), "stage": r.get("stage"), "threshold": f"score {r['operator']} {r['value']}",
                "binding": r.get("binding"), "fired": r["fires"], "cells_over": r["cells_over"]}
               for r in sorted(evald, key=lambda r: r.get("priority", 0))]
    evidence = [governing["id"], AOI_ID] + ([src["chunk_hash"]] if src.get("chunk_hash") else [])

    rid = f"ds:{WRITE_TENANT}:governance/recommendation/flood-{obs[:13] or 'latest'}"
    rec = {"schema": "governance.recommendation", "id": rid,
           "title": f"{base}: {governing['stage']} flood risk — {governing['binding']} action",
           "summary": narrative, "proposed_action": governing.get("proposed_action", ""),
           "rationale": (f"Deterministic evaluation of {len(rules)} approved governance.rule against {n} "
                         f"{FACT_SCHEMA} cells; governing rule '{governing.get('rule_id')}' fired "
                         f"(worst-cell score {mx} {governing['operator']} {governing['value']}); the advisory "
                         f"text was drafted by {'the '+model if model else 'a template'} from that rule + the prediction."),
           "evidence": [e for e in evidence if e], "confidence": 0.72 if not fell_back else 0.65,
           "method": "rule", "produced_by": f"component://{CID}@{VER}",
           "score": governing.get("priority", 0), "created_at": obs, "tenant": WRITE_TENANT,
           "skill": CID, "reasoning": REASONING, "narrative_model": model, "narrative_templated": fell_back,
           "for_facts": {"schema": FACT_SCHEMA, "metric": METRIC, "aoi": AOI_ID, "n": n, "max": mx, "mean": mean,
                         "breached": governing["cells_over"], "rainfall_mm": rain, "observed_at": obs, "cascade": cascade},
           "citation": {"rule": governing["id"], "authority": governing.get("authority"),
                        "chunk_hash": src.get("chunk_hash"), "instrument": src.get("citation")}}
    _call("/v1/write", {"scope": "governance.recommendation", "id": rid, "object": rec,
                        "valid_time": {"from": obs}, "asserted_at": obs})

    print(f"RECOMMENDATION [{base}]: {rec['title']}")
    print(f"  {narrative}")
    print(f"  method=rule · governing='{governing.get('rule_id')}' · worst-cell {mx} · "
          f"{governing['cells_over']}/{n} cells breached · narrative={model or 'templated'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
