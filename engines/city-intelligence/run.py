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
    }
    present = {"risk.flood": flood, "cell.crowd_density": crowd, "vision.detection": cctv,
               "governance.recommendation": recs}
    sources = [s for s in ["risk.flood", "cell.crowd_density", "vision.detection", "governance.recommendation"] if present[s]]

    sysmsg = ("You are the City Intelligence Analyst for AirawatOS, advising a mass-gathering (Kumbh) control room. "
              "Answer the operator's question using ONLY the FACTS JSON provided — these are live, governed records "
              "joined across the flood, crowd, CCTV and recommendation layers. State concrete numbers (cells, people, "
              "scores, sites) from the facts; never invent any. Name the worst site(s) and, if the question asks who "
              "to mobilise, list the response roles implied by the open recommendations (e.g. crowd marshals, "
              "sanitation, medical & transit, transport, parking, police). Output ONLY the final briefing for the "
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
