#!/usr/bin/env python3
"""Learning Engine — the LEARN edge of the decision loop.

Aggregates intelligence.effectiveness (the measured outcomes of past decisions) BY intervention + target measure
into an intelligence.learning signal: how many times it was tried, how often it improved things, the mean effect.
Turns individual outcomes into institutional knowledge ("reduce-inflow on crowd density: 3/3 improved, mean
-71%"). Honest — reports the sample size; early signals are thin and labelled so. Confined; broker only.
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

def main():
    if not _os.environ.get("RUN_TOKEN"):
        print("Learning Engine — aggregate intelligence.effectiveness → intelligence.learning"); return 0
    T = _os.environ.get("TENANT") or _os.environ.get("WRITE_TENANT") or ""
    if not T: print("no tenant"); return 1
    groups = {}
    for e in _q("intelligence.effectiveness"):
        key = (e.get("intervention") or "unknown", e.get("target_schema") or "unknown")
        g = groups.setdefault(key, {"samples": 0, "improved": 0, "worsened": 0, "deltas": []})
        g["samples"] += 1
        if e.get("direction") == "improved": g["improved"] += 1
        elif e.get("direction") == "worsened": g["worsened"] += 1
        if isinstance(e.get("delta_pct"), (int, float)): g["deltas"].append(e["delta_pct"])
    now = _now(); n = 0
    for (interv, target), g in groups.items():
        mean_delta = round(sum(g["deltas"]) / len(g["deltas"]), 1) if g["deltas"] else None
        rate = round(g["improved"] / g["samples"], 2) if g["samples"] else None
        verdict = ("effective" if (rate or 0) >= 0.7 else "mixed" if (rate or 0) >= 0.4 else "ineffective") if g["samples"] >= 1 else "unknown"
        conf = "provisional (n<3)" if g["samples"] < 3 else "emerging" if g["samples"] < 8 else "established"
        lid = f"ds:{T}:intelligence/learning/{interv}--{target.replace('.','_')}"
        _call("/v1/write/batch", {"items": [{"scope": "intelligence.learning", "id": lid, "object": {
            "schema": "intelligence.learning", "id": lid, "tenant": T,
            "intervention": interv, "target_schema": target, "samples": g["samples"],
            "improved": g["improved"], "worsened": g["worsened"], "improve_rate": rate,
            "mean_delta_pct": mean_delta, "verdict": verdict, "evidence_strength": conf,
            "note": ("Aggregated from OBSERVED before/after outcomes (intelligence.effectiveness). Small samples "
                     "are provisional; and observed change is not proof of causation."),
            "observed_at": now,
            "prov": {"component": "component://ds:airawatdeveloper:engine/learning-engine", "method": "aggregate-effectiveness", "at": now}}}],
            "asserted_at": now})
        n += 1
    print(f"[learning-engine] tenant={T}: {len(groups)} intervention×target groups → {n} intelligence.learning signals", flush=True)
    return 0
if __name__ == "__main__": raise SystemExit(main())
