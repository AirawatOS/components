#!/usr/bin/env python3
"""Flood × Crowd Coordinator — the cross-function join.

Two functions, two data sources: the FLOOD function writes risk.flood (predicted ponding per H3 cell); the CROWD
function writes cell.crowd_density (headcount/density per H3 cell, per time). Neither can see the other. This engine
INTERSECTS them: a cell that is FLOODING (level high/severe) AND CROWDED (elevated band / density) at the same time
is the dangerous overlap — a packed ghat about to go under. For each such cell it emits a cited
governance.recommendation (thin crowd + hold inflow + relocate riverside assets). AI flags; a commander decides.

Runs headless over the confined broker (same contract the drivers use): read /v1/query, write /v1/write/batch.
"""
import os as _os, json as _json, time as _t


def _now():
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())


def _api():
    return _os.environ.get("AIRAWAT_API", "http://localhost:8080")


def _call(path, body):
    import urllib.request
    req = urllib.request.Request(_api() + path, data=_json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": _os.environ.get("RUN_TOKEN", "")}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return _json.loads(r.read() or b"{}")


def _q(scope):
    return [r.get("object") or r for r in (_call("/v1/query", {"scope": scope, "limit": 200000}).get("results") or [])]


def _h3(o):
    g = o.get("space") or o.get("cell_ref") or {}
    return g.get("h3_id") or g.get("h3"), g


# flood/crowd thresholds — a real deployment reads these from a governance.rule; kept inline for the coordinator.
FLOOD_LEVELS = {"high", "severe"}
CROWD_BANDS = {"busy", "heavy", "dense", "crowded", "critical", "danger", "warning"}   # anything above "normal"


def coordinate(tenant):
    flood = {}
    for o in _q("risk.flood"):
        h, _ = _h3(o)
        if h and (o.get("level") in FLOOD_LEVELS):
            flood[h] = o
    # peak crowd per cell (the series carries many (cell,time) rows)
    crowd = {}
    for o in _q("cell.crowd_density"):
        h, g = _h3(o)
        if not h:
            continue
        hc = o.get("headcount") or 0
        cur = crowd.get(h)
        if (not cur) or hc > (cur.get("headcount") or 0):
            crowd[h] = o
    now = _now()
    recs = 0
    for h, f in flood.items():
        c = crowd.get(h)
        if not c:
            continue
        band = (c.get("band") or "").lower()
        dens = c.get("density_sqm") or 0
        if band not in CROWD_BANDS and dens < 3.0:            # crowd not elevated → not a coordination case
            continue
        site = c.get("site") or "ghat"
        head = c.get("headcount")
        rid = f"ds:{tenant}:governance/recommendation/floodcrowd-{h}"
        summary = (f"FLOOD × CROWD overlap at {site} (cell {h}): flood risk {f.get('level')} (score {f.get('score')}) "
                   f"coincides with a {band or 'high-density'} crowd (~{head} people). Thin the crowd from this ghat, "
                   f"hold inflow at the approach gates, and relocate riverside toilets/vendors before water rises. "
                   f"Dispatch: crowd marshals + sanitation + transit control.")
        # governance.recommendation is a strict schema: method∈{rule,statistical,model,human}; no space/site fields.
        # The cell + site live in the summary; the JOINED evidence (flood id + crowd id) is the cross-function proof.
        ctx_write(rid, {"schema": "governance.recommendation", "id": rid, "tenant": tenant,
            "title": f"Flood×Crowd — thin crowd & relocate assets at {site}",
            "summary": summary, "proposed_action": "thin-crowd-and-relocate",
            "rationale": f"risk.flood={f.get('level')}({f.get('score')}) ∩ crowd band={band or 'high'} headcount={head}",
            "evidence": [f.get("id"), c.get("id")], "confidence": round(min(1.0, 0.6 + 0.1 * (dens or 0)), 3),
            "method": "rule", "score": f.get("score"),
            "produced_by": "component://ds:airawatdeveloper:engine/flood-crowd-coordinator",
            "created_at": now})
        recs += 1
    return {"flood_cells": len(flood), "crowd_cells": len(crowd), "overlap_recommendations": recs}


def ctx_write(oid, obj):
    _call("/v1/write/batch", {"items": [{"scope": obj.get("schema"), "id": oid, "object": obj}], "asserted_at": _now()})


def main():
    if not _os.environ.get("RUN_TOKEN"):
        print("Flood×Crowd Coordinator — coordinate(flood ∩ crowd) → governance.recommendation"); return 0
    tenant = _os.environ.get("TENANT") or _os.environ.get("WRITE_TENANT") or ""
    if not tenant:
        try:
            tenant = (_call("/v1/context", {}) or {}).get("tenant") or ""
        except Exception:
            pass
    if not tenant:
        print("no tenant"); return 1
    r = coordinate(tenant)
    print(f"[flood-crowd-coordinator] tenant={tenant}: flood={r['flood_cells']} crowd={r['crowd_cells']} "
          f"→ {r['overlap_recommendations']} cross-function recommendations", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
