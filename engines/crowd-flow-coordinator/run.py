#!/usr/bin/env python3
"""Crowd-Flow Coordinator — sense (CCTV + crowd) → diagnose (inflow>outflow) → recommend (reduce inflow).

Two functions feed it: the crowd-count function writes cell.crowd_density (headcount per H3 cell, per time), and the
CCTV function writes vision.detection (person-count per camera/cell). This engine finds the cell where a CAMERA has
detected a hotspot AND the crowd series is climbing into danger/critical, computes the net INFLOW imbalance from the
steepest rise (Δheadcount/hr = inflow minus outflow), and emits a cited governance.recommendation to REDUCE INFLOW.
It does not act — a human commander decides, then transport/parking/police execute. Confined: only I/O is the broker.
"""
import os as _os, json as _json, time as _t


def _now(): return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
def _api(): return _os.environ.get("AIRAWAT_API", "http://localhost:8080")


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
    return g.get("h3_id") or g.get("h3")


HOT_BANDS = {"danger", "critical", "warning", "caution"}          # crowd is elevated / climbing to unsafe
INFLOW_MIN = float(_os.environ.get("INFLOW_MIN", "4000"))          # min net inflow/hr to flag an imbalance
SAFE_DENSITY = float(_os.environ.get("SAFE_DENSITY", "2.0"))       # people/m² considered safe


def coordinate(tenant):
    # crowd series per cell (sorted by time)
    series = {}
    for o in _q("cell.crowd_density"):
        h = _h3(o)
        if h: series.setdefault(h, []).append(o)
    # latest CCTV detection per cell (the camera that saw the hotspot)
    cctv = {}
    for o in _q("vision.detection"):
        h = _h3(o)
        if not h: continue
        cur = cctv.get(h)
        if not cur or (o.get("observed_at") or "") > (cur.get("observed_at") or ""):
            cctv[h] = o
    now = _now(); recs = 0
    for h, rows in series.items():
        cam = cctv.get(h)
        if not cam:                                              # only where a CAMERA confirms the hotspot
            continue
        rows.sort(key=lambda r: r.get("observed_at") or "")
        peak = max(rows, key=lambda r: r.get("headcount") or 0)
        band_peak = (peak.get("band") or "").lower()
        if band_peak not in HOT_BANDS:
            continue
        # net inflow = steepest hour-on-hour rise (Δheadcount = inflow − outflow)
        best_d = 0; imb = None
        for a, b in zip(rows, rows[1:]):
            d = (b.get("headcount") or 0) - (a.get("headcount") or 0)
            if d > best_d:
                best_d = d; imb = b
        if best_d < INFLOW_MIN or imb is None:
            continue
        site = peak.get("site") or "ghat"
        dens = peak.get("density_sqm")
        rid = f"ds:{tenant}:governance/recommendation/crowdflow-{h}"
        summary = (f"REDUCE INFLOW at {site} (cell {h}): CCTV detected a hotspot ({cam.get('count')} people in frame) and "
                   f"the crowd is climbing to {band_peak} — net inflow +{best_d:,}/hr (arrivals exceeding departures), "
                   f"peaking at ~{peak.get('headcount'):,} people ({dens}/m² vs {SAFE_DENSITY}/m² safe). Hold inbound buses, "
                   f"redirect arriving vehicles to outer parking, and meter the approach gates until outflow catches up.")
        _call("/v1/write/batch", {"items": [{"scope": "governance.recommendation", "id": rid, "object": {
            "schema": "governance.recommendation", "id": rid, "tenant": tenant,
            "title": f"Crowd-flow — reduce inflow at {site}",
            "summary": summary, "proposed_action": "reduce-inflow",
            "rationale": f"cctv_count={cam.get('count')} band={band_peak} net_inflow_per_hr={best_d} density={dens}",
            "evidence": [imb.get("id"), cam.get("id")], "confidence": 0.8, "method": "rule",
            "score": round(min(1.0, best_d / 12000.0), 3),
            "produced_by": "component://ds:airawatdeveloper:engine/crowd-flow-coordinator",
            "created_at": now}}], "asserted_at": now})
        recs += 1
    return {"cells": len(series), "cctv_cells": len(cctv), "reduce_inflow_recommendations": recs}


def main():
    if not _os.environ.get("RUN_TOKEN"):
        print("Crowd-Flow Coordinator — CCTV × crowd → inflow imbalance → reduce-inflow recommendation"); return 0
    tenant = _os.environ.get("TENANT") or _os.environ.get("WRITE_TENANT") or ""
    if not tenant:
        print("no tenant"); return 1
    r = coordinate(tenant)
    print(f"[crowd-flow-coordinator] tenant={tenant}: crowd cells={r['cells']} cctv cells={r['cctv_cells']} "
          f"→ {r['reduce_inflow_recommendations']} reduce-inflow recommendations", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
