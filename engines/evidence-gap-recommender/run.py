#!/usr/bin/env python3
"""Evidence-Gap Recommender — the "find out more" move.

Scans detected hotspots (risk.flood high/severe that coincide with elevated cell.crowd_density) and flags the ones
with NO corroborating evidence — no CCTV detection on the cell — as candidates to VERIFY rather than act on blind.
For each, it writes a governance.recommendation with proposed_action=gather-evidence (dispatch a field officer or a
camera check), citing the uncorroborated hotspot. Managing uncertainty is itself a recommendation. Confined; broker only.
"""
import os as _os, json as _json, time as _t
def _now(): return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
def _api(): return _os.environ.get("AIRAWAT_API", "http://localhost:8080")
def _call(path, body):
    import urllib.request
    req = urllib.request.Request(_api()+path, data=_json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": _os.environ.get("RUN_TOKEN","")}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r: return _json.loads(r.read() or b"{}")
def _q(scope): return [r.get("object") or r for r in (_call("/v1/query", {"scope": scope, "limit": 200000}).get("results") or [])]
def _h3(o):
    g = o.get("space") or o.get("cell_ref") or {}; return g.get("h3_id") or g.get("h3")

def main():
    if not _os.environ.get("RUN_TOKEN"):
        print("Evidence-Gap Recommender — flag uncorroborated hotspots → gather-evidence recommendations"); return 0
    T = _os.environ.get("TENANT") or _os.environ.get("WRITE_TENANT") or ""
    if not T: print("no tenant"); return 1
    flood = {h: o for o in _q("risk.flood") for h in [_h3(o)] if h and o.get("level") in ("high","severe")}
    crowd = {}
    for o in _q("cell.crowd_density"):
        h = _h3(o)
        if not h: continue
        if h not in crowd or (o.get("headcount") or 0) > (crowd[h].get("headcount") or 0): crowd[h] = o
    cctv = {h for o in _q("vision.detection") for h in [_h3(o)] if h}
    now = _now(); recs = 0
    # hotspot = flood∩crowd(elevated); GAP = no CCTV on the cell (uncorroborated)
    hotspots = [(h, flood[h], crowd[h]) for h in flood if h in crowd
                and ((crowd[h].get("band") or "").lower() not in ("normal","") or (crowd[h].get("density_sqm") or 0) >= 3.0)]
    uncorroborated = [(h,f,c) for (h,f,c) in hotspots if h not in cctv]
    uncorroborated.sort(key=lambda t: -(t[1].get("score") or 0))
    for h, f, c in uncorroborated[:25]:
        site = c.get("site") or "hotspot"; rid = f"ds:{T}:governance/recommendation/evidencegap-{h}"
        _call("/v1/write/batch", {"items": [{"scope": "governance.recommendation", "id": rid, "object": {
            "schema": "governance.recommendation", "id": rid, "tenant": T,
            "title": f"Verify before acting — uncorroborated hotspot at {site}",
            "summary": (f"Cell {h} at {site} is flagged FLOODING ({f.get('level')} {f.get('score')}) and CROWDED "
                        f"(~{c.get('headcount')} people, {c.get('band')}) but has NO CCTV corroboration. Before an "
                        f"operational order, gather evidence: dispatch a field officer to verify, or task a camera "
                        f"check. Managing this uncertainty is the recommended next step."),
            "proposed_action": "gather-evidence",
            "rationale": f"flood={f.get('level')}({f.get('score')}) ∩ crowd band={c.get('band')} · CCTV corroboration=none",
            "evidence": [f.get("id"), c.get("id")], "confidence": 0.55, "method": "rule",
            "score": f.get("score"), "produced_by": "component://ds:airawatdeveloper:engine/evidence-gap-recommender",
            "created_at": now}}], "asserted_at": now})
        recs += 1
    print(f"[evidence-gap-recommender] tenant={T}: hotspots={len(hotspots)} uncorroborated={len(uncorroborated)} "
          f"→ {recs} gather-evidence recommendations", flush=True)
    return 0
if __name__ == "__main__": raise SystemExit(main())
