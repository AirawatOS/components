"""Waste Compliance Engine — rank areas by source-segregation compliance → corrective-action recommendation.

Reads the governed waste.segregation_compliance measures (a rate 0..1 per unit at a time), ranks the units below
a segregation floor worst-first, and emits ONE governance.recommendation naming the weakest areas and the
corrective action. Deterministic + auditable (no fabricated numbers — it computes over whatever compliance facts
exist). AI recommends; a sanitation authority admits it (decision spine, ADR-0029).
"""
import time as _t

CID = "ds:airawatdeveloper:engine/waste-compliance-engine"
CVER = "0.1.0"
def _now(): return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())

def h_waste_compliance_rank(ctx):
    tenant = ctx["tenant"]; now = _now(); body = ctx.get("body") or {}
    floor = float(body.get("floor", 0.7))                     # source-segregation floor (fraction)
    top_k = int(body.get("top_k", 8))
    comps = ctx["kfind"]("waste.segregation_compliance") or []
    vals = [c for c in comps if isinstance(c.get("value"), (int, float))]
    if not vals:
        return {"error": "no waste.segregation_compliance data — ingest/seed the sanitation-collection feed first"}, None
    n = len(vals); mean = sum(c["value"] for c in vals) / n
    below = sorted([c for c in vals if c["value"] < floor], key=lambda c: c["value"])
    worst = below[:top_k]
    def _area(c): return (c.get("space") or {}).get("h3") or (c.get("subject") or "").rsplit("/", 1)[-1]
    worst_ids = [_area(c) for c in worst]

    if not below:
        return {"units": n, "mean": round(mean, 3), "below_floor": 0, "recommendations": 0,
                "note": f"all units at/above the {int(floor*100)}% floor"}, None

    rid = f"ds:{tenant}:governance/recommendation/waste-segregation-compliance"
    rationale = (f"{len(below):,} of {n:,} areas are below the {int(floor*100)}% source-segregation floor "
                 f"(city mean {round(mean*100)}%). Weakest: {', '.join(str(w) for w in worst_ids[:5])}. "
                 f"Corrective action: a targeted IEC (awareness) drive plus graded enforcement in these areas, "
                 f"with a re-inspection after 30 days. Ranked worst-first by measured segregation compliance.")
    ctx["kwrite"](rid, {
        "schema": "governance.recommendation", "id": rid, "tenant": tenant,
        "title": f"Raise waste segregation — {len(below):,} weak areas",
        "summary": (f"{len(below):,} areas below the {int(floor*100)}% segregation floor (city mean {round(mean*100)}%); "
                    f"run an IEC + enforcement drive in the worst {len(worst)}."),
        "proposed_action": (f"Source-segregation IEC + graded enforcement drive in the {len(worst)} weakest areas "
                            f"({', '.join(str(w) for w in worst_ids[:5])}…); re-inspect in 30 days."),
        "rationale": rationale, "evidence": [c["id"] for c in worst],
        "confidence": 0.6, "score": round(sum(floor - c["value"] for c in below), 3),
        "method": "statistical", "produced_by": CID, "created_at": now})
    return {"units": n, "mean": round(mean, 3), "below_floor": len(below), "recommendations": 1,
            "worst": worst_ids}, None

HANDLERS = {"waste-compliance-rank": h_waste_compliance_rank}

def _selftest():
    comps = [{"id": f"ds:x:waste/segregation_compliance/c{i}", "subject": f"ds:x:civic/household/cell-{i}",
              "value": v, "space": {"h3": f"h{i}"}} for i, v in enumerate([0.9, 0.55, 0.4, 0.8, 0.3])]
    store = {}
    ctx = {"tenant": "x", "body": {"floor": 0.7}, "kwrite": lambda i, o: store.__setitem__(i, o),
           "kfind": lambda s: {"waste.segregation_compliance": comps}.get(s, [])}
    res, err = h_waste_compliance_rank(ctx)
    assert err is None and res["below_floor"] == 3 and res["worst"][0] == "h4", res    # 0.3 is worst
    rec = list(store.values())[0]
    assert rec["schema"] == "governance.recommendation" and "weak areas" in rec["title"], rec
    print("selftest OK:", {k: res[k] for k in ("units", "mean", "below_floor", "recommendations")}, "| worst:", res["worst"][:3])
    print("  ", rec["summary"])

def main():
    print("Waste Compliance Engine — handlers:", ", ".join(sorted(HANDLERS))); _selftest(); return 0

if __name__ == "__main__":
    raise SystemExit(main())
