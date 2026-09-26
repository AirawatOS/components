#!/usr/bin/env python3
"""City Intelligence — the City Scan Analyst, a certified in-process engine (verb: city-ask).

Grounded Q&A over the tenant's GOVERNED map layers. It reads the live records behind the layers via its own read
grants (ctx["kfind"], attributed component://…), computes the cross-layer facts (e.g. cells that are BOTH flooding
AND crowded, people exposed, worst sites, open recommendations), grounds the confined model (ctx["ai_complete"]) on
ONLY those facts, and returns {answer, sources, facts}. The AI never acts and never invents — every figure it states
is a real record the operator can find on the map (ADR-0029: AI informs, the human commander decides).
"""
import json, re

HOT_FLOOD = ("high", "severe")


def _h3(o):
    g = o.get("space") or o.get("cell_ref") or {}
    return g.get("h3_id") or g.get("h3")


def _age_minutes(iso):
    """Minutes since an ISO8601 observed_at, or None if unparseable."""
    import time, calendar
    try:
        t = time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
        return round((time.time() - calendar.timegm(t)) / 60.0)
    except Exception:
        return None


def evidence_quality(facts_by_schema, coverage):
    """Compose the multi-dimensional QUALITY of the evidence per source layer — coverage %, freshness,
    observed-vs-inferred (method), and mean confidence — from records the platform already produces. This is
    the essay's core point: not a single confidence number, but the quality of the evidence behind a conclusion.
    `coverage` maps source_schema → a city.coverage record (coverage-qa)."""
    out = {}
    for schema, rows in facts_by_schema.items():
        n = len(rows)
        ages = [a for a in (_age_minutes(r.get("observed_at") or "") for r in rows) if a is not None]
        methods = {}
        confs = []
        for r in rows:
            m = (r.get("method") or "").lower()
            if m in ("measured", "sensor", "human", "vision-count", "person-detection", "count"):
                k = "observed"                                   # a direct measurement / detection
            elif m in ("model", "statistical", "rule", "sim-gaussian", "simulation") or m.startswith("sim"):
                k = "inferred"                                   # estimated / modelled / simulated
            else:
                k = "unattributed"                               # no method declared — itself an evidence-quality signal
            methods[k] = methods.get(k, 0) + 1
            c = r.get("confidence")
            if isinstance(c, (int, float)):
                confs.append(c)
        cv = coverage.get(schema) or {}
        out[schema] = {
            "records": n,
            "coverage_pct": cv.get("coverage_pct") or cv.get("coverage") or cv.get("coverage_percent"),
            "gaps": cv.get("gaps"),
            "freshness_min": (min(ages) if ages else None),      # freshest observation, minutes old
            "staleness_min": (max(ages) if ages else None),      # oldest observation still in play
            "observed_vs_inferred": methods,                     # e.g. {"observed": 12, "inferred": 40}
            "mean_confidence": (round(sum(confs) / len(confs), 3) if confs else cv.get("mean_confidence")),
        }
    return out


def h_ask(ctx):
    body = ctx.get("body") or {}
    question = (body.get("question") or body.get("q") or "").strip()[:500]
    if not question:
        return None, "question required"
    kfind = ctx["kfind"]

    flood = {}
    for o in kfind("risk.flood"):
        h = _h3(o)
        if h and o.get("level") in HOT_FLOOD:
            flood[h] = o
    crowd = {}
    for o in kfind("cell.crowd_density"):
        h = _h3(o)
        if not h:
            continue
        hc = o.get("headcount") or 0
        if h not in crowd or hc > (crowd[h].get("headcount") or 0):
            crowd[h] = o
    cctv = {}
    for o in kfind("vision.detection"):
        h = _h3(o)
        if h and (h not in cctv or (o.get("observed_at") or "") > (cctv[h].get("observed_at") or "")):
            cctv[h] = o
    recs = [o for o in kfind("governance.recommendation")
            if any(k in (o.get("id") or "") for k in ("floodcrowd-", "crowdflow-")) and "incident" not in (o.get("id") or "")]

    # ── EVIDENCE QUALITY (the essay's point: not one confidence number, but the QUALITY of the evidence) ──
    # Compose what the platform already computes — coverage (city.coverage), freshness (observed_at age),
    # observed-vs-inferred (method), and per-source confidence — into ONE structure the decider can interrogate.
    cov = {}
    for o in kfind("city.coverage"):
        src = o.get("source_schema") or o.get("layer") or o.get("noun")
        if src:
            cov[src] = o
    eq = evidence_quality({"risk.flood": list(flood.values()), "cell.crowd_density": list(crowd.values()),
                           "vision.detection": list(cctv.values())}, cov)

    overlap = []
    for h, f in flood.items():
        c = crowd.get(h)
        if not c:
            continue
        band = (c.get("band") or "").lower()
        if band in ("normal", "") and (c.get("density_sqm") or 0) < 3.0:
            continue
        overlap.append({"h3": h, "site": c.get("site") or "ghat", "flood_level": f.get("level"),
                        "flood_score": f.get("score"), "headcount": c.get("headcount"),
                        "density": c.get("density_sqm"), "band": c.get("band"),
                        "cctv": (cctv.get(h) or {}).get("count")})
    overlap.sort(key=lambda x: -(x["flood_score"] or 0))
    total_exposed = sum(x["headcount"] or 0 for x in overlap)

    facts = {
        "flood_cells_high_or_severe": len(flood),
        "crowd_cells": len(crowd),
        "cctv_hotspot_cells": len(cctv),
        "flood_and_crowd_overlap_cells": len(overlap),
        "people_exposed_in_overlap": total_exposed,
        "top_overlaps": overlap[:8],
        "open_recommendations": [{"title": r.get("title"), "action": r.get("proposed_action"),
                                  "rationale": r.get("rationale")} for r in recs[:8]],
        "evidence_quality": eq,
    }
    present = {"risk.flood": flood, "cell.crowd_density": crowd, "vision.detection": cctv,
               "governance.recommendation": recs}
    sources = [s for s in ["risk.flood", "cell.crowd_density", "vision.detection", "governance.recommendation"] if present[s]]

    sysmsg = ("You are the City Intelligence Analyst for AirawatOS, advising a mass-gathering (Kumbh) control room. "
              "Answer the operator's question using ONLY the FACTS JSON provided — these are live, governed records "
              "joined across the flood, crowd, CCTV and recommendation layers. State concrete numbers (cells, people, "
              "scores, sites) from the facts; never invent any. Name the worst site(s) and, if the question asks who "
              "to mobilise, list the response roles implied by the open recommendations (e.g. crowd marshals, "
              "sanitation, medical & transit, transport, parking, police). ALWAYS end with a one-line "
              "'Evidence quality:' note drawn from facts.evidence_quality — coverage %, freshest/oldest observation "
              "age, observed-vs-inferred mix, and confidence — so the commander sees how good the evidence is, not "
              "just the conclusion. Output ONLY the final briefing for the "
              "commander: begin immediately with a one-line headline, then hyphen bullets. Do NOT show any reasoning, "
              "do NOT restate the question or these instructions, and never write phrases like 'the user wants' or "
              "'the facts say'. If the facts show no overlap, say so plainly. You inform; a human commander decides "
              "and signs the action.")
    msgs = [{"role": "system", "content": sysmsg},
            {"role": "user", "content": "FACTS:\n" + json.dumps(facts)[:11000] + "\n\nQUESTION: " + question}]
    try:
        ans = (ctx["ai_complete"](msgs, 900) or "").strip()
    except Exception as e:
        return None, "the AI model is unavailable: " + str(e)[:160]
    ans = re.sub(r"(?is)<think>.*?</think>", "", ans).strip()          # drop hidden reasoning blocks
    _tells = ("the user wants", "the facts", "system prompt", "need to", "we need", "let me", "i should")
    if any(t in ans[:120].lower() for t in _tells):                    # reasoning leaked → keep the last block
        blocks = [b for b in re.split(r"\n\s*\n", ans) if b.strip()]
        if blocks:
            ans = blocks[-1].strip()
    return {"answer": ans, "sources": sources, "facts": facts}, None


HANDLERS = {"city-ask": h_ask}

if __name__ == "__main__":
    print("City Intelligence — the City Scan Analyst. Verb: city-ask (grounded Q&A over governed map layers).")
