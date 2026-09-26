#!/usr/bin/env python3
"""Crowd Density Sensor (SIMULATION) — a GENERIC confined driver. Its only I/O is the broker.

Generates a peak-day crowd-density timeline for gathering hotspots supplied via CONFIG (CROWD_SITES) and writes
it to the governed cell.crowd_density schema (temporal dated snapshots). EVERY record's prov.source="simulation"
— scenario/demo data, NOT a real feed. In production this driver is replaced by a fusion of CCTV people-counting,
bus GPS and telco density writing the SAME schema. NO city/event hardcoded — hotspots come from config, so the one
driver serves any city's mass-gathering tabletop or demo.
"""
import json, math, os, urllib.request
import h3

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
RES = int(os.environ.get("CROWD_RES", "9"))
DATE = os.environ.get("SIM_DATE", "2027-01-01")
PEAK_HOUR = int(os.environ.get("PEAK_HOUR", "13"))
FRAMES = [f"{DATE}T{h:02d}:00:00Z" for h in range(0, 19)]
SCENARIO = os.environ.get("SCENARIO", "surge")                    # baseline | surge
SURGE_SITES = {s.strip() for s in os.environ.get("SURGE_SITES", "").split(",") if s.strip()}
# hotspots come from CONFIG — no coordinates in the code
SITES = json.loads(os.environ.get("CROWD_SITES", "[]"))


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def context():
    req = urllib.request.Request(API + "/v1/context", headers={"X-Run-Token": TOKEN})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def band(d):
    return ("critical" if d >= 6 else "danger" if d >= 4.5 else "caution" if d >= 3
            else "busy" if d >= 1.5 else "normal")


def time_curve(hour, peak_hour, sigma, base=0.04):
    g = math.exp(-((hour - peak_hour) ** 2) / (2 * sigma ** 2))
    return base + (1 - base) * g


def main():
    if not SITES:
        print("crowd-density-sim: no CROWD_SITES configured — nothing to simulate. "
              'Set CROWD_SITES=[{"name","lat","lng","k","peak_density"}].'); return 0
    T = context().get("tenant") or os.environ.get("WRITE_TENANT", "")
    prov = {"source": "simulation", "simulator": "crowd-density-sim@0.1.0", "scenario": SCENARIO,
            "note": "SIMULATED demo data — not a real feed"}
    surge_all = not SURGE_SITES
    CELL_M2 = h3.cell_area(h3.latlng_to_cell(SITES[0]["lat"], SITES[0]["lng"], RES), unit="m^2") * 0.068
    written = 0; peak_total = 0
    for hs in SITES:
        site = hs["name"]; k = int(hs.get("k", 4)); peak = float(hs.get("peak_density", 5.0))
        spikes = SCENARIO == "surge" and (surge_all or site in SURGE_SITES)
        center = h3.latlng_to_cell(hs["lat"], hs["lng"], RES)
        for cell in h3.grid_disk(center, k):
            ring = h3.grid_distance(center, cell)
            spatial = math.exp(-(ring ** 2) / (2 * (k / 1.6) ** 2))
            lat, lng = h3.cell_to_latlng(cell)
            boundary = [[b[1], b[0]] for b in h3.cell_to_boundary(cell)]
            for t in FRAMES:
                hour = int(t[11:13]); sig = 2.6 if SCENARIO == "baseline" else 2.2
                inten = time_curve(hour, PEAK_HOUR, sig)
                if spikes and hour in (PEAK_HOUR, PEAK_HOUR + 1) and ring <= 1:
                    inten = min(1.0, inten * 1.35)                # the sharp peak-hour pinch-point spike
                d = round(peak * spatial * inten, 2)
                if d < 0.3:
                    continue
                head = int(d * CELL_M2)
                if hour == PEAK_HOUR:
                    peak_total += head
                oid = f"ds:{T}:cell/crowd_density/{cell}-{t[:13]}"
                obj = {"schema": "cell.crowd_density", "id": oid, "tenant": T, "layer": "L2",
                       "space": {"h3_id": cell, "centroid": [lng, lat], "boundary": boundary},
                       "observed_at": t, "density_sqm": d, "headcount": head, "capacity": round(CELL_M2 * 4),
                       "band": band(d), "method": "sim-gaussian", "site": site, "prov": prov}
                call("/v1/write", {"scope": "cell.crowd_density", "id": oid, "object": obj})
                written += 1
    print(f"crowd-density-sim [{SCENARIO}]: wrote {written} cell.crowd_density rows to {T} "
          f"across {len(SITES)} configured site(s); ~peak hour {PEAK_HOUR}:00 headcount ≈ {peak_total:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
