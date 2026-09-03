"""Welfare Deprivation Engine (ADR-0069 slice 2) — the real equity weight.

For each household, build a multidimensional deprivation index in [0,1]:
  * ECONOMIC dimension — nightlight radiance per capita in the household's cell, percentile-ranked across the
    city; econ_dep = 1 - prosperity_percentile (dark & dense ⇒ deprived). A MODELLED proxy (low confidence)
    until real income/consumption replaces it — the same slot, method flips to metered and the signal sharpens.
  * SERVICE dimensions — for each governed capability the household is below its floor on, the normalised
    shortfall (shortfall ÷ floor), in [0,1].
index = mean intensity across the available dimensions (MPI-style). deprived[] = the dimensions below their floor.
Writes welfare.deprivation, which the prioritisation-engine reads to rank the worst-off first. Degrades: with no
nightlight it scores on service dimensions alone.
"""
import time as _t

CID = "ds:airawatdeveloper:engine/welfare-deprivation-engine"
CVER = "0.1.0"
def _now(): return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
def _clamp(x, lo=0.0, hi=1.0): return max(lo, min(hi, x))

def _percentiles(values):
    """Map each value to its percentile rank in [0,1] (fraction of values <= it). Ties share the upper rank."""
    if not values:
        return {}
    srt = sorted(values); n = len(srt)
    import bisect
    return {v: bisect.bisect_right(srt, v) / n for v in set(values)}

def _cell_of(hh):
    return (hh.get("space") or {}).get("h3")

def h_deprivation_scan(ctx):
    tenant = ctx["tenant"]; now = _now()
    households = ctx["kfind"]("civic.household") or []
    gaps = ctx["kfind"]("welfare.gap") or []
    floors = ctx["kfind"]("welfare.floor") or []
    nightlight = ctx["kfind"]("econ.nightlight") or []
    pops = ctx["kfind"]("demographics.population") or []

    cap_ids = sorted({f.get("capability") for f in floors if f.get("capability")})
    gap_by = {(g.get("subject"), g.get("capability")): g for g in gaps}
    nl_by = {}
    for r in nightlight:
        c = (r.get("space") or {}).get("h3")
        if c: nl_by[c] = r.get("avg_radiance", r.get("value"))
    pop_by = {}
    for p in pops:
        c = (p.get("space") or {}).get("h3")
        if c: pop_by[c] = p.get("population")

    # pass 1: light per capita per household → collect for percentile ranking
    lpc = {}
    for hh in households:
        c = _cell_of(hh); light = nl_by.get(c)
        pop = pop_by.get(c) or hh.get("size")
        if light is not None and pop:
            lpc[hh["id"]] = light / pop
    pr = _percentiles(list(lpc.values()))
    have_econ = bool(lpc)

    written = 0; deprived_hh = 0
    for hh in households:
        dims = {}                       # name -> (intensity 0..1, is_deprived)
        if hh["id"] in lpc:
            prosperity = pr.get(lpc[hh["id"]], 0.5)
            econ_dep = _clamp(1.0 - prosperity)
            dims["economic"] = (round(econ_dep, 3), econ_dep >= 0.5)
        for cap in cap_ids:
            g = gap_by.get((hh["id"], cap))
            if not g:
                continue
            sf = g.get("shortfall") or 0.0; fv = g.get("floor_value")
            norm = _clamp(sf / fv) if (fv and sf > 0) else 0.0
            dims[cap] = (round(norm, 3), sf > 0)
        if not dims:
            continue
        index = round(sum(i for i, _ in dims.values()) / len(dims), 4)
        deprived = [k for k, (_, d) in dims.items() if d]
        if deprived: deprived_hh += 1
        did = f"ds:{tenant}:welfare/deprivation/{hh['id'].rsplit('/', 1)[-1]}"
        ctx["kwrite"](did, {
            "schema": "welfare.deprivation", "id": did, "tenant": tenant,
            "subject": hh["id"], "subject_kind": "household", "observed_at": now,
            "index": index, "shortfalls": {k: i for k, (i, _) in dims.items()},
            "deprived": deprived, "headcount_weight": hh.get("size"),
            "method": "modelled" if ("economic" in dims) else "derived",
            "confidence": 0.5 if have_econ else 0.6,
            "derived_from": [g.get("id") for (s, cp), g in gap_by.items() if s == hh["id"]],
            "prov": {"method": "MPI-style mean intensity; economic=nightlight-per-capita percentile" if have_econ
                     else "MPI-style mean intensity; service dimensions only (no nightlight)",
                     "dimensions": list(dims.keys()),
                     "produced_by": {"component": {"id": CID, "version": CVER}}, "observed_at": now}})
        written += 1
    return {"households": len(households), "with_economic": len(lpc),
            "deprivation_written": written, "deprived_households": deprived_hh}, None

HANDLERS = {"welfare-deprivation-scan": h_deprivation_scan}

def _selftest():
    # 3 households in 2 cells; cell A bright (rich), cell B dark (poor); water gaps make hh3 doubly deprived
    hh = [{"id": "ds:x:civic/household/h1", "size": 100, "space": {"h3": "A"}},
          {"id": "ds:x:civic/household/h2", "size": 100, "space": {"h3": "A"}},
          {"id": "ds:x:civic/household/h3", "size": 100, "space": {"h3": "B"}}]
    nl = [{"space": {"h3": "A"}, "avg_radiance": 50.0}, {"space": {"h3": "B"}, "avg_radiance": 2.0}]
    pops = [{"space": {"h3": "A"}, "population": 200}, {"space": {"h3": "B"}, "population": 100}]
    floors = [{"id": "ds:x:welfare/floor/water", "capability": "welfare.capability/water"}]
    gaps = [{"id": "ds:x:welfare/gap/w-h3", "subject": "ds:x:civic/household/h3",
             "capability": "welfare.capability/water", "shortfall": 135, "floor_value": 135}]
    store = {}
    ctx = {"tenant": "x", "kwrite": lambda i, o: store.__setitem__(i, o),
           "kfind": lambda s: {"civic.household": hh, "welfare.gap": gaps, "welfare.floor": floors,
                               "econ.nightlight": nl, "demographics.population": pops}.get(s, [])}
    res, err = h_deprivation_scan(ctx)
    assert err is None and res["deprivation_written"] == 3, res
    d3 = store["ds:x:welfare/deprivation/h3"]; d1 = store["ds:x:welfare/deprivation/h1"]
    # h3 is in the dark cell AND below the water floor → strictly more deprived than h1 (bright, no gap)
    assert d3["index"] > d1["index"], (d1["index"], d3["index"])
    assert "economic" in d3["deprived"] and "welfare.capability/water" in d3["deprived"], d3
    print("selftest OK:", {k: res[k] for k in ("households", "with_economic", "deprivation_written", "deprived_households")})
    print("  h1 (bright, no gap) index:", d1["index"], "| h3 (dark + water gap) index:", d3["index"], "deprived:", d3["deprived"])

def main():
    print("Welfare Deprivation Engine — handlers:", ", ".join(sorted(HANDLERS))); _selftest(); return 0

if __name__ == "__main__":
    raise SystemExit(main())
