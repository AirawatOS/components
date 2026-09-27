#!/usr/bin/env python3
"""air-recommendation-expert (aos SDK engine): air.hotspot → cited governance.recommendation.

The MISSING LINK in the Air Intelligence loop. hotspot-detection writes an air.hotspot; the governed
subscription {on: air.hotspot, do: air.recommend} fires THIS engine, which turns the flagged hotspot into a
governance.recommendation (cited to the hotspot) via the SDK decision spine (ctx.recommend). That
recommendation then trips the {on: governance.recommendation, do: decision.route} subscription →
decision-router opens a role-addressed action in the Inbox. So: sense → detect → RECOMMEND → route → act.

Event-driven: the dispatcher passes the triggering air.hotspot as the body; if invoked bare (a manual sweep)
it scans recent air.hotspot facts. Idempotent — the recommendation id is derived from the hotspot cell +
observation hour, so re-firing the same hotspot updates rather than duplicates. No fabrication: only real
air.hotspot records produce recommendations; a clean grid yields none.
"""
from aos import engine, ctx

CID = "ds:airawatdeveloper:engine/air-recommendation-expert"

# Severity → the mandated municipal action (the recommendation's proposed_action). Auditable, deterministic.
SEV_ACTION = {
    "critical": "Deploy source enforcement in the affected cells now and issue a public-health advisory; escalate to the pollution-control board.",
    "warning":  "Dispatch a field inspection to the hotspot and notify the pollution-control board for follow-up.",
    "info":     "Flag for monitoring; verify with a field reading if the exceedance persists.",
}
SEV_CONF = {"critical": 0.9, "warning": 0.75, "info": 0.6}


def _hotspot_from(body):
    """Accept the several shapes the automation dispatcher / a caller may pass the triggering record in."""
    for k in ("hotspot", "subject", "object", "record"):
        v = body.get(k)
        if isinstance(v, dict) and v.get("schema") == "air.hotspot":
            return v
    return body if body.get("schema") == "air.hotspot" else None


@engine.verb("air-recommend", implements="air.recommend@0.1.0",
             reads=["air.hotspot"], writes=["governance.recommendation"])
def air_recommend(body):
    body = body or {}
    hs = _hotspot_from(body)
    hotspots = [hs] if hs else [h.get("object") or h for h in ctx.facts("air.hotspot").list()]
    made = []
    for h in hotspots:
        if not isinstance(h, dict) or h.get("schema") != "air.hotspot":
            continue
        sev = h.get("severity", "info")
        sp = h.get("space") or {}
        cell = sp.get("h3", "?")
        val, thr, ncells = h.get("value"), h.get("threshold"), h.get("cell_count", 1)
        obs = h.get("observed_at") or ctx.now()
        title = f"{str(sev).title()} PM2.5 hotspot at {cell} — {ncells} cell(s) over threshold"
        rationale = (f"air.hotspot severity '{sev}': PM2.5 peak {val} µg/m³ ≥ CPCB threshold {thr} across "
                     f"{ncells} contiguous cell(s) (method {h.get('method')}). Recommending the mandated "
                     f"action for this band.")
        rid = f"ds:{ctx.tenant}:governance/recommendation/air-hotspot-{cell}-{str(obs)[:13]}"
        rec = ctx.recommend(title, SEV_ACTION.get(sev, SEV_ACTION["info"]),
                            evidence=[h.get("id")], rationale=rationale,
                            confidence=SEV_CONF.get(sev, 0.6), produced_by=CID, id=rid)
        made.append(rec["id"])
    return {"recommendations": made, "count": len(made)}


HANDLERS = engine.handlers()
