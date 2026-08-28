#!/usr/bin/env python3
"""air-quality-advisory SKILL (INT-0002): Facts × Rules → cited governance.recommendation.

HYBRID reasoning: a DETERMINISTIC core evaluates the approved governance.rule for air.quality/pm25 against
the AOI's measured facts and selects the highest-PRIORITY rule that fires (auditable, no model); the LLM
then writes the human-readable advisory grounded ONLY in that fired rule + the measured stats. The
recommendation's SUBSTANCE is rule-derived (method="rule"), so it stays auditable — the model only phrases
it. Confined: the only I/O is the broker. AI recommends; an authority admits it into a governance.decision
downstream (decision-router → inbox). No synthetic data: no fired rule ⇒ no recommendation; model
unavailable ⇒ a templated rationale, LOUDLY flagged.
"""
import json, os, time, urllib.request, urllib.error

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/kanpur-core")
FACT_SCHEMA = os.environ.get("FACT_SCHEMA", "air.quality")
METRIC = os.environ.get("METRIC", "pm25")
WRITE_TENANT = os.environ.get("WRITE_TENANT", "kmc")
REASONING = os.environ.get("REASONING", "hybrid")        # deterministic | assisted | hybrid
CID = "ds:airawat:skill/air-quality-advisory"
VER = "0.1.0"
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


def _clean(txt):
    """Extract ONLY the final advisory from a reasoning model's output (kimi thinks out loud). Prefer the
    <advisory>…</advisory> the prompt asks for; else strip <think> blocks + drop analysis paragraphs."""
    import re
    m = re.search(r"<advisory>\s*(.+?)\s*</advisory>", txt, re.S | re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S | re.I).strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n", txt) if p.strip()]
    META = ("let me", "i need", "i'm given", "i should", "i can use", "draft:", "sentence ", "wait",
            "double-check", "hmm", "actually", "maybe", "that's", "constraints", "numbers i", "note:",
            "okay", "first,", "so ", "- ", "1.", "2.", "3.")
    good = [p for p in paras if not any(p.lower().startswith(w) for w in META)]
    for p in reversed(good):                                   # the model's LAST clean advisory-like paragraph
        if len(p) > 40 and ("µg/m" in p or "PM2.5" in p or "band" in p.lower() or "advisory" in p.lower()):
            return re.sub(r"\s+", " ", p).strip()
    return re.sub(r"\s+", " ", (good[-1] if good else txt)).strip()[:600]


def _narrative(base, rule, stats):
    """LLM advisory grounded ONLY in the fired rule + stats. Returns (text, model, fell_back)."""
    try:
        instr = open(os.path.join(HERE, "SKILL.md")).read()
    except Exception:
        instr = ""
    prompt = (f"{instr}\n\n---\nArea: {base}\n"
              f"Governing rule that fired — band '{rule.get('stage')}', PM2.5 {rule['operator']} {rule['value']} µg/m³, "
              f"authority {rule.get('authority')}, {rule.get('binding')}. Mandated action: {rule.get('proposed_action')}.\n"
              f"Measured now — PM2.5 mean {stats['mean']}, max {stats['max']} µg/m³ across {stats['n']} cells.\n"
              f"Write the advisory now — 2-3 short sentences. Output ONLY the final advisory wrapped in "
              f"<advisory>…</advisory>, with NO analysis, preamble, drafts, or notes before or after it.")
    try:
        res = _call("/v1/complete", {"messages": [{"role": "user", "content": prompt}]}, timeout=60)
        txt = _clean((res.get("content") or "").strip())
        if txt:
            return txt, res.get("model"), False
    except Exception:
        pass
    return (f"PM2.5 across {base} (mean {stats['mean']}, max {stats['max']} µg/m³ over {stats['n']} cells) crosses "
            f"the '{rule.get('stage')}' threshold (pm25 {rule['operator']} {rule['value']}). "
            f"{rule.get('authority')} · {rule.get('binding')}: {rule.get('proposed_action')} "
            f"[advisory narrative model unavailable — templated]", None, True)


def main():
    aoi = _call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object")
    if not aoi:
        print(f"FAIL: no AOI at {AOI_ID}"); return 1
    base = aoi.get("name", "AOI")

    rules = [r.get("object") or r for r in _call("/v1/query", {"scope": "governance.rule", "limit": 1000}).get("results", [])]
    # ONLY approved/live rules drive a recommendation — a `proposed` rule (freshly extracted) awaits human approval.
    rules = [r for r in rules if r.get("applies_to") == FACT_SCHEMA and r.get("metric") == METRIC
             and r.get("status") != "proposed"]
    if not rules:
        print(f"no approved governance.rule for {FACT_SCHEMA}.{METRIC} — nothing to apply."); return 0

    cells = [c.get("object") or c for c in _call("/v1/query", {"scope": FACT_SCHEMA, "limit": 5000}).get("results", [])]
    vals = [c.get(METRIC) for c in cells if isinstance(c.get(METRIC), (int, float))]
    if not vals:
        print(f"no {FACT_SCHEMA}.{METRIC} facts to evaluate — run the driver + interpolation first."); return 0
    n = len(vals); mean = round(sum(vals) / n, 1); mx = round(max(vals), 1); mn = round(min(vals), 1)
    stats = {"n": n, "mean": mean, "max": mx, "min": mn}

    evald = [{**r, "fires": _match(mean, r["operator"], r["value"]),
              "cells_over": sum(1 for v in vals if _match(v, r["operator"], r["value"]))} for r in rules]
    fired = [r for r in evald if r["fires"]]
    governing = max(fired, key=lambda r: r.get("priority", 0)) if fired else None
    obs = cells[0].get("observed_at", "")

    if not governing:
        print(f"{base}: PM2.5 mean {mean} µg/m³ over {n} cells — no rule threshold crossed. No recommendation."); return 0

    if REASONING == "deterministic":
        narrative, model, fell_back = (f"PM2.5 across {base} (mean {mean}, max {mx} µg/m³ over {n} cells) crosses the "
                                       f"'{governing['stage']}' threshold. {governing.get('authority')} · "
                                       f"{governing.get('binding')}: {governing.get('proposed_action')}", None, False)
    else:
        narrative, model, fell_back = _narrative(base, governing, stats)

    src = governing.get("source") or {}
    cascade = [{"rule": r.get("rule_id"), "stage": r.get("stage"), "threshold": f"pm25 {r['operator']} {r['value']}",
                "binding": r.get("binding"), "fired": r["fires"], "cells_over": r["cells_over"]}
               for r in sorted(evald, key=lambda r: r.get("priority", 0))]
    evidence = [governing["id"], AOI_ID] + ([src["chunk_hash"]] if src.get("chunk_hash") else [])

    rid = f"ds:{WRITE_TENANT}:governance/recommendation/air-quality-{obs[:13] or 'latest'}"
    rec = {"schema": "governance.recommendation", "id": rid,
           "title": f"{base}: {governing['stage']} air quality — {governing['binding']} action",
           "summary": narrative, "proposed_action": governing.get("proposed_action", ""),
           "rationale": (f"Deterministic evaluation of {len(rules)} approved governance.rule against {n} "
                         f"{FACT_SCHEMA} cells; governing rule '{governing.get('rule_id')}' fired "
                         f"(pm25 {governing['operator']} {governing['value']}); the advisory text was drafted "
                         f"by {'the '+model if model else 'a template'} from that rule + the measured stats."),
           "evidence": [e for e in evidence if e], "confidence": 1.0 if not fell_back else 0.9,
           "method": "rule", "produced_by": f"component://{CID}@{VER}",
           "score": governing.get("priority", 0), "created_at": obs, "tenant": WRITE_TENANT,
           # skill provenance (INT-0002) — how this recommendation was reasoned
           "skill": CID, "reasoning": REASONING, "narrative_model": model, "narrative_templated": fell_back,
           "for_facts": {"schema": FACT_SCHEMA, "metric": METRIC, "aoi": AOI_ID, "n": n,
                         "mean": mean, "max": mx, "min": mn, "observed_at": obs, "cascade": cascade},
           "citation": {"rule": governing["id"], "authority": governing.get("authority"),
                        "chunk_hash": src.get("chunk_hash"), "instrument": src.get("citation")}}
    _call("/v1/write", {"scope": "governance.recommendation", "id": rid, "object": rec,
                        "valid_time": {"from": obs}, "asserted_at": obs})

    print(f"RECOMMENDATION [{base}]: {rec['title']}")
    print(f"  {narrative}")
    print(f"  method=rule · governing='{governing.get('rule_id')}' · cited to rule"
          + (f" + source chunk {src['chunk_hash'][:18]}…" if src.get("chunk_hash") else "")
          + f" · narrative={model or 'templated'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
