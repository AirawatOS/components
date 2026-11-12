#!/usr/bin/env python3
"""air-hotspot engine (aos SDK): air.quality (PM2.5 grid) → air.hotspot (clustered, tiered pollution hotspots).

Confined — its only I/O is the SDK's ctx (NO egress: pure computation over registry facts). It reads the
latest air.quality cell-measures, classifies each H3 cell against CPCB 24h PM2.5 breakpoints, clusters
CONTIGUOUS exceedance cells via H3 adjacency (union-find over grid_disk), and writes ONE air.hotspot per
cluster — severity = the worst tier in the cluster, located at the peak cell (the space block is the map pin).
No synthetic data: if the grid is empty or clean it writes nothing and says so, loudly.

aos SDK: registry read/write + tenant resolution are the SDK's ctx; each hotspot is a bitemporal ASSERTION
valid from its observation window (ctx.write_many valid_from_field), asserted at the run time.
"""
import os
from aos import driver, ctx

# WHICH cells are hotspots is RELATIVE (0.3.0): the top (100-PCTL)% of the city's own PM2.5 surface — a fixed 60 µg/m³
# made a broadly-elevated city one giant cluster (staging Bengaluru: 8,336 of 11,118 cells). A FLOOR keeps a clean city
# clean: cells within CPCB "Good" (≤30) are never hotspots, however they rank.
PCTL = float(os.environ.get("HOTSPOT_PCTL", "95"))
FLOOR = float(os.environ.get("HOTSPOT_FLOOR", "30"))
# HOW BAD a hotspot is stays ABSOLUTE — CPCB 24h PM2.5 breakpoints at the cluster's peak (checked high→low):
# Very Poor/Severe >120 = critical, Poor 91-120 = warning, anything else that made the top percentile = info.
TIERS = [("critical", 120.0), ("warning", 90.0), ("info", 60.0)]


def percentile(values, p):
    """Nearest-rank percentile (p in 0..100) — exact on the data, no interpolation."""
    v = sorted(values)
    if not v:
        return None
    k = max(0, min(len(v) - 1, int(-(-p * len(v) // 100)) - 1))
    return v[k]


def _tier(pm):
    for name, lo in TIERS:
        if pm is not None and pm >= lo:
            return name, lo
    return ("info", None) if pm is not None else (None, None)


@driver.main
def run():
    import h3
    now = ctx.now()
    T = ctx.tenant
    if not T:
        print("FAIL: could not resolve the run tenant (/v1/context)."); return 1

    HOUR = os.environ.get("HOUR", "").strip()              # optional: detect on a SPECIFIC air.quality hour (else latest)
    latest = {}                                            # h3 -> (pm25, observed_at, centroid, res)
    for r in ctx.facts("air.quality").list():
        o = r.get("object") or r
        sp = o.get("space") or {}
        cell, pm = sp.get("h3"), o.get("pm25")
        if not cell or pm is None:
            continue
        obs = o.get("observed_at") or ""
        if HOUR and obs != HOUR:                            # targeting one hour → ignore all others
            continue
        if cell not in latest or obs > latest[cell][1]:
            latest[cell] = (pm, obs, sp.get("centroid"), sp.get("res"))
    if not latest:
        print("no air.quality cells found — run station-interpolation first. Nothing written."); return 0

    cut = percentile([v[0] for v in latest.values()], PCTL)
    thr = max(cut, FLOOR)
    exc = {c: v for c, v in latest.items() if v[0] >= thr}          # the city's own top (100-PCTL)%, above the floor
    if not exc:
        print(f"grid clean: {len(latest)} cells, none at/above the p{PCTL:g} cut {cut:.1f} and the floor "
              f"{FLOOR:g} µg/m³. Nothing written."); return 0

    parent = {c: c for c in exc}                            # union-find over H3 adjacency
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[ra] = rb
    for c in exc:
        for nb in h3.grid_disk(c, 1):
            if nb in exc and nb != c:
                union(c, nb)
    clusters = {}
    for c in exc:
        clusters.setdefault(find(c), []).append(c)

    facts = []
    for cells in clusters.values():
        peak = max(cells, key=lambda c: exc[c][0])          # peak cell drives location + severity
        pm_peak, obs_peak, centroid, res = exc[peak]
        sev, lo = _tier(pm_peak)
        cen = centroid or list(h3.cell_to_latlng(peak)[::-1])          # [lng, lat] fallback from the cell
        obs_list = [exc[c][1] for c in cells if exc[c][1]]
        rid = f"ds:{T}:air/hotspot/{peak}-{(obs_peak or '')[:13]}"
        fact = {"schema": "air.hotspot", "id": rid,
                "space": {"h3": peak, "res": res, "centroid": [round(cen[0], 5), round(cen[1], 5)]},
                "severity": sev, "pollutant": "pm25",
                "value": round(pm_peak, 1), "threshold": round(thr, 1), "cell_count": len(cells),
                "window": {"from": min(obs_list) if obs_list else obs_peak,
                           "to": max(obs_list) if obs_list else obs_peak},
                "observed_at": obs_peak or now,
                "method": "p%g-cluster (top %g%% of the city surface, floor %g µg/m³; severity = CPCB tier at the peak)"
                          % (PCTL, 100 - PCTL, FLOOR)}
        facts.append(fact)

    # bitemporal: each hotspot valid from its observation time; asserted at the run time; one batched round-trip
    written = ctx.write_many(facts, valid_from_field="observed_at", asserted_at=now) if facts else 0

    by_sev = {}
    for cells in clusters.values():
        s = _tier(exc[max(cells, key=lambda c: exc[c][0])][0])[0]
        by_sev[s] = by_sev.get(s, 0) + 1
    print(f"detected {written} air.hotspot cluster(s) from {len(exc)} cells at/above p{PCTL:g} = {thr:.1f} µg/m³ "
          f"(of {len(latest)} grid cells) — {by_sev}")
    return 0


if __name__ == "__main__":
    raise SystemExit(driver.run())
