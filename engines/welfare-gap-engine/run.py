"""Welfare Gap Engine (ADR-0069 slice 2 · ADR-0070).

Per household × capability: shortfall = floor - delivered level (respecting the floor's direction), written as a
welfare.gap fact, weighted toward the worst-off. Per ward × capability: a stat.estimate that carries HONEST
uncertainty — a Wilson score interval (sampling) blended with a coverage term (how much of the ward we actually
measured). The blend is the ADR-0070 v1 analytic rule: at full coverage the interval is Wilson; as coverage → 0
the interval widens to [0,1] because the unmeasured households could be anything. A governed gate (min coverage,
max interval width) sets `publishable` — below it the number is provisional, never a headline.

Pure helpers are unit-testable without a registry (see _selftest). v2 replaces the analytic blend with a
Monte-Carlo simulation carrying a shared per-(method,source) bias term for correlation.
"""
import math, time as _t

CID = "ds:airawatdeveloper:engine/welfare-gap-engine"
CVER = "0.1.1"
Z90 = 1.6449                                              # z for a 90% interval
GATE_DEFAULT = {"min_coverage": 0.30, "max_interval_width": 0.30, "ci_level": 0.90}

def _now(): return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
def _clamp(x, lo=0.0, hi=1.0): return max(lo, min(hi, x))

# per-record reliability by measurement method (used when a record has no explicit `confidence`).
# Drives the effective-sample-size deflation: a wall of low-reliability estimates cannot buy a tight interval.
METHOD_REL = {"metered": 0.95, "measured": 0.90, "surveyed": 0.80, "declared": 0.60,
              "modelled": 0.50, "interpolated": 0.50, "estimated": 0.40, "imputed": 0.25, "derived": 0.60}
def _reliability(L):
    r = L.get("confidence")
    return r if r is not None else METHOD_REL.get(L.get("method") or "", 0.40)

# ---- pure statistics (ADR-0070 v1) ---------------------------------------
def wilson(k, n, z=Z90):
    """Wilson score interval for k successes in n Bernoulli trials — the SAMPLING uncertainty."""
    if n <= 0:
        return 0.0, 1.0
    phat = k / n; z2 = z * z; denom = 1.0 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))
    return _clamp(center - margin), _clamp(center + margin)

def coverage_blend(lo, hi, coverage):
    """Widen a sampling interval for COVERAGE ignorance: the (1-coverage) fraction we never measured could be
    anything in [0,1]. Convex blend — full coverage keeps (lo,hi); zero coverage → [0,1]. This is the term that
    stops 200-of-5000 measurements from masquerading as a confident ward number."""
    c = _clamp(coverage)
    return c * lo, c * hi + (1 - c) * 1.0

def gate(coverage, ci_low, ci_high, g):
    return bool(coverage >= g["min_coverage"] and (ci_high - ci_low) <= g["max_interval_width"])

# ---- domain helpers -------------------------------------------------------
def _ward_of(hh):
    sp = hh.get("space") or {}
    return hh.get("ward") or sp.get("boundary_ref") or "city"

def _pick_floor(floors, hh):
    """Choose the applicable floor for a household. v1: honour applies_when.settlement_class if both are known,
    else prefer the strongest legal_force, else the first. (Overlapping-jurisdiction precedence is ADR-0070 OQ.)"""
    if not floors:
        return None
    sc = (hh.get("settlement_class") or (hh.get("space") or {}).get("settlement_class"))
    if sc:
        for f in floors:
            aw = f.get("applies_when") or {}
            classes = aw.get("settlement_class") or []
            if sc in classes:
                return f
    order = {"statutory": 0, "slb": 1, "contractual": 2, "recommended": 3}
    return sorted(floors, key=lambda f: order.get(f.get("legal_force"), 9))[0]

def _below(level_value, floor):
    """Is this delivered level below the dignity floor, and by how much? Respects the floor's bound/direction."""
    thr = floor.get("threshold")
    bound = floor.get("bound") or "min"                  # min = floor (need >= thr); max = ceiling (need <= thr)
    if level_value is None or thr is None:
        return None, None
    if bound == "max":
        return (level_value > thr), max(0.0, level_value - thr)
    return (level_value < thr), max(0.0, thr - float(level_value))

# ---- the handler ----------------------------------------------------------
def h_gap_scan(ctx):
    tenant = ctx["tenant"]; now = _now(); body = ctx.get("body") or {}
    g = dict(GATE_DEFAULT); g.update({k: v for k, v in body.get("gate", {}).items() if k in GATE_DEFAULT})

    households = ctx["kfind"]("civic.household") or []
    levels     = ctx["kfind"]("welfare.service_level") or []
    floors_all = ctx["kfind"]("welfare.floor") or []
    depriv     = {d.get("subject"): d for d in (ctx["kfind"]("welfare.deprivation") or [])}

    floors_by_cap = {}
    for f in floors_all:
        floors_by_cap.setdefault(f.get("capability"), []).append(f)
    # level lookup by (household subject, capability) -> latest value
    lvl = {}
    for L in levels:
        lvl[(L.get("subject"), L.get("capability"))] = L
    # the CONNECTION register — a recorded no/illegal connection is ZERO service (worst-off), NOT missing data.
    conn = {}
    for c in (ctx["kfind"]("infra.service_connection") or []):
        conn[(c.get("serves"), c.get("capability"))] = c
    INACTIVE = {"none", "illegal", "disconnected", "abandoned"}

    # ward × capability accumulators
    acc = {}
    ward_hh = {}  # ward -> total household count (the coverage denominator)
    gaps_written = 0
    for hh in households:
        ward = _ward_of(hh); ward_hh[ward] = ward_hh.get(ward, 0) + 1
        for cap, floors in floors_by_cap.items():
            floor = _pick_floor(floors, hh)
            if not floor:
                continue
            bound = floor.get("bound") or "min"
            L = lvl.get((hh.get("id"), cap))
            c = conn.get((hh.get("id"), cap))
            # decide the observation: a numeric measured level WINS; else the connection register gives ACCESS
            # (only for min-floors, i.e. more-service-is-better). No level AND no connection record = unobserved.
            if L is not None:
                below, shortfall = _below(L.get("value"), floor)
                if below is None:
                    continue
                level_value = L.get("value"); src_id = L.get("id"); rel = _reliability(L); meth = L.get("method") or "unknown"
            elif c is not None and bound == "min":
                st = c.get("status")
                if st in INACTIVE:                            # register SAYS no service → worst-off, fully below floor
                    below = True; shortfall = floor.get("threshold") or 0; level_value = 0
                    src_id = c.get("id"); rel = 0.9; meth = "no-connection"
                elif st == "active":                          # has access; level unknown → meets the access floor
                    below = False; shortfall = 0.0; level_value = None
                    src_id = c.get("id"); rel = 0.7; meth = "connected"
                else:
                    continue                                  # applied/other → unknown
            else:
                continue                                      # genuinely not observed
            # prioritarian weight: deprivation index if we have it, else household size (persons), else 1
            d = depriv.get(hh.get("id"))
            weight = (d.get("index") if d else None) or hh.get("size") or 1
            gid = f"ds:{tenant}:welfare/gap/{cap.split('/')[-1]}-{hh['id'].rsplit('/', 1)[-1]}"
            ctx["kwrite"](gid, {
                "schema": "welfare.gap", "id": gid, "tenant": tenant,
                "subject": hh["id"], "subject_kind": "household", "observed_at": now,
                "capability": cap, "floor_ref": floor.get("id"),
                "floor_value": floor.get("threshold"), "level_value": level_value,
                "shortfall": shortfall, "weight": weight, "weighted": shortfall * weight,
                "method": "derived", "confidence": rel,
                "derived_from": [floor.get("id"), src_id],
                "prov": {"method": meth, "derived_from": [floor.get("id"), src_id],
                         "produced_by": {"component": {"id": CID, "version": CVER}}, "observed_at": now}})
            gaps_written += 1
            a = acc.setdefault((ward, cap), {"n_obs": 0, "k_below": 0, "w_below": 0.0, "w_total": 0.0, "conf_sum": 0.0, "methods": {}})
            a["n_obs"] += 1; a["k_below"] += 1 if below else 0
            a["w_total"] += weight; a["w_below"] += weight if below else 0.0; a["conf_sum"] += rel
            a["methods"][meth] = a["methods"].get(meth, 0) + 1

    # ward × capability rollups → stat.estimate with honest error bars
    estimates = 0; published = 0
    for (ward, cap), a in acc.items():
        n = a["n_obs"]; k = a["k_below"]; N = max(n, ward_hh.get(ward, n))
        value = (a["w_below"] / a["w_total"]) if a["w_total"] else 0.0   # PERSON-weighted fraction below floor
        rel = (a["conf_sum"] / n) if n else 0.0
        n_eff = max(1.0, n * rel)                                        # deflate for data quality (v1 correlation proxy)
        w_lo, w_hi = wilson(value * n_eff, n_eff)                        # interval on the effective sample
        coverage = n / N if N else 0.0
        ci_lo, ci_hi = coverage_blend(w_lo, w_hi, coverage)
        method_mix = {m: round(c / n, 3) for m, c in a["methods"].items()} if n else {}
        pub = gate(coverage, ci_lo, ci_hi, g)
        eid = f"ds:{tenant}:stat/estimate/welfare-gap-{cap.split('/')[-1]}-{ward}"
        ctx["kwrite"](eid, {
            "schema": "stat.estimate", "id": eid, "tenant": tenant,
            "subject": ward, "subject_kind": "ward", "observed_at": now,
            "metric": "welfare.gap.below_floor_fraction", "capability": cap,
            "value": round(value, 4), "unit": "fraction_of_persons",
            "ci_low": round(ci_lo, 4), "ci_high": round(ci_hi, 4), "ci_level": g["ci_level"],
            "coverage": round(coverage, 4), "n_observed": n, "n_target": N,
            "method_mix": method_mix, "model_version": "gap-v1-analytic", "publishable": pub,
            "method": "analytic", "derived_from": [f"welfare.gap@ward:{ward}:cap:{cap}"],
            "prov": {"method": "person-weighted; wilson(n_eff)+coverage-blend", "gate": g,
                     "note": "v1 analytic; n_eff = n x mean_reliability (correlation proxy); v2 = Monte-Carlo",
                     "n_effective": round(n_eff, 1), "mean_reliability": round(rel, 3),
                     "persons_below": round(a["w_below"], 1), "persons_total": round(a["w_total"], 1),
                     "derived_from": [f"welfare.gap@ward:{ward}:cap:{cap}"],
                     "produced_by": {"component": {"id": CID, "version": CVER}}, "observed_at": now}})
        estimates += 1; published += 1 if pub else 0
    return {"households": len(households), "gaps": gaps_written,
            "estimates": estimates, "publishable": published, "provisional": estimates - published}, None

HANDLERS = {"welfare-gap-scan": h_gap_scan}

# ---- self-test: exercise the pure math + shapes with an in-memory ctx (NOT persisted city data) ----
def _selftest():
    assert wilson(0, 0) == (0.0, 1.0)
    lo, hi = wilson(20, 100); assert 0.13 < lo < 0.15 and 0.26 < hi < 0.29, (lo, hi)   # 90% Wilson ≈ [0.143,0.273]
    assert coverage_blend(0.2, 0.3, 1.0) == (0.2, 0.3)
    b = coverage_blend(0.2, 0.3, 0.0); assert b == (0.0, 1.0), b
    lo2, hi2 = coverage_blend(0.2, 0.3, 0.5); assert abs(lo2 - 0.1) < 1e-9 and abs(hi2 - 0.65) < 1e-9, (lo2, hi2)
    # tiny in-memory fixture: 5 households, water floor 135 LPCD, 3 measured (2 below), ward of 5
    floors = [{"id": "ds:x:welfare/floor/water", "schema": "welfare.floor", "capability": "welfare.capability/water",
               "threshold": 135, "unit": "LPCD", "bound": "min", "legal_force": "slb"}]
    hh = [{"id": f"ds:x:civic/household/h{i}", "schema": "civic.household", "size": 4, "ward": "w1"} for i in range(5)]
    lv = [{"id": "ds:x:welfare/service_level/l0", "subject": "ds:x:civic/household/h0", "capability": "welfare.capability/water", "value": 51, "method": "modelled", "confidence": 0.5},
          {"id": "ds:x:welfare/service_level/l1", "subject": "ds:x:civic/household/h1", "capability": "welfare.capability/water", "value": 90, "method": "modelled", "confidence": 0.5},
          {"id": "ds:x:welfare/service_level/l2", "subject": "ds:x:civic/household/h2", "capability": "welfare.capability/water", "value": 150, "method": "metered", "confidence": 0.95}]
    # connection register: h3 has NO water connection (status none) → must count as below floor (worst-off),
    # NOT be skipped as unobserved. h4 has neither a level nor a connection → genuinely unobserved (skipped).
    conns = [{"id": "ds:x:infra/service_connection/c3", "serves": "ds:x:civic/household/h3",
              "capability": "welfare.capability/water", "status": "none"}]
    store = {}
    def kfind(s): return {"civic.household": hh, "welfare.service_level": lv, "welfare.floor": floors,
                          "welfare.deprivation": [], "infra.service_connection": conns}.get(s, [])
    ctx = {"tenant": "x", "body": {}, "kfind": kfind, "kwrite": lambda oid, o: store.__setitem__(oid, o)}
    res, err = h_gap_scan(ctx)
    # 4 observed (h0,h1,h2 by level + h3 by no-connection); h4 unobserved. 3 below (h0,h1 low + h3 no-conn).
    assert err is None and res["gaps"] == 4 and res["estimates"] == 1, res
    g3 = store["ds:x:welfare/gap/water-h3"]
    assert g3["level_value"] == 0 and g3["shortfall"] == 135 and g3["prov"]["method"] == "no-connection", g3
    est = [o for o in store.values() if o["schema"] == "stat.estimate"][0]
    # person-weighted below-fraction = 3/4 = 0.75 (equal sizes); coverage 4/5 = 0.8
    assert est["value"] == 0.75 and est["coverage"] == 0.8, est
    assert est["n_observed"] == 4 and est["n_target"] == 5
    assert est["ci_low"] < est["value"] < est["ci_high"], est
    print("selftest OK:", {k: res[k] for k in ("households", "gaps", "estimates", "publishable", "provisional")})
    print("  sample estimate:", {k: est[k] for k in ("value", "ci_low", "ci_high", "coverage", "n_observed", "n_target", "publishable", "method_mix")})

def main():
    print("Welfare Gap Engine — handlers:", ", ".join(sorted(HANDLERS)))
    _selftest(); return 0

if __name__ == "__main__":
    raise SystemExit(main())
