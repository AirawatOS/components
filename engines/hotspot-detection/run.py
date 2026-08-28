#!/usr/bin/env python3
"""air-hotspot engine: air.quality (PM2.5 grid) -> air.hotspot (clustered, tiered pollution hotspots).

Confined -- its only I/O is the kernel broker (NO egress: pure computation over registry facts). It reads
the latest air.quality cell-measures, classifies each H3 cell against CPCB 24h PM2.5 breakpoints, clusters
CONTIGUOUS exceedance cells via H3 adjacency (union-find over grid_disk), and writes ONE air.hotspot per
cluster -- severity = the worst tier in the cluster, located at the peak cell (the space block is the map
pin). No synthetic data: if the grid is empty or clean it writes nothing and says so, loudly.
"""
import json, os, time, urllib.request
import h3

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]

# CPCB 24h PM2.5 breakpoints -> the governed three-tier air.hotspot severity (checked high->low).
# Moderate 61-90 = info, Poor 91-120 = warning, Very Poor/Severe >120 = critical. Below 60 is not a hotspot.
TIERS = [("critical", 120.0), ("warning", 90.0), ("info", 60.0)]


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def _tenant():
    req = urllib.request.Request(API + "/v1/context", headers={"X-Run-Token": TOKEN})
    return (json.loads(urllib.request.urlopen(req, timeout=20).read()) or {}).get("tenant")


def _tier(pm):
    for name, lo in TIERS:
        if pm is not None and pm >= lo:
            return name, lo
    return None, None


def main():
    T = _tenant()
    if not T:
        print("FAIL: could not resolve the run tenant (/v1/context)."); return 1

    rows = _call("/v1/query", {"scope": "air.quality", "limit": 100000}).get("results", [])
    latest = {}                                            # h3 -> (pm25, observed_at, centroid, res)
    for r in rows:
        o = r.get("object") or r
        sp = o.get("space") or {}
        cell, pm = sp.get("h3"), o.get("pm25")
        if not cell or pm is None:
            continue
        obs = o.get("observed_at") or ""
        if cell not in latest or obs > latest[cell][1]:
            latest[cell] = (pm, obs, sp.get("centroid"), sp.get("res"))
    if not latest:
        print("no air.quality cells found -- run station-interpolation first. Nothing written."); return 0

    exc = {c: v for c, v in latest.items() if _tier(v[0])[0]}     # exceedance cells (>= info)
    if not exc:
        print(f"grid clean: {len(latest)} cells, none above the info threshold "
              f"(PM2.5 {TIERS[-1][1]:.0f}). Nothing written."); return 0

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

    written = 0
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
                "value": round(pm_peak, 1), "threshold": lo, "cell_count": len(cells),
                "window": {"from": min(obs_list) if obs_list else obs_peak,
                           "to": max(obs_list) if obs_list else obs_peak},
                "observed_at": obs_peak or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "method": "cpcb-breakpoint-cluster", "tenant": T}
        _call("/v1/write", {"scope": "air.hotspot", "id": rid, "object": fact,
                            "valid_time": {"from": fact["observed_at"]},
                            "asserted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        written += 1

    by_sev = {}
    for cells in clusters.values():
        s = _tier(exc[max(cells, key=lambda c: exc[c][0])][0])[0]
        by_sev[s] = by_sev.get(s, 0) + 1
    print(f"detected {written} air.hotspot cluster(s) from {len(exc)} exceedance cells "
          f"(of {len(latest)} grid cells) -- {by_sev}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
