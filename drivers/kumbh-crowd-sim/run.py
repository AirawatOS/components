#!/usr/bin/env python3
"""Kumbh Crowd Sensor (SIMULATION) — a confined driver. Its only I/O is the broker.

Generates a peak-day crowd-density timeline for the Nashik–Trimbakeshwar mela and writes it to the
governed `cell.crowd_density` schema (temporal dated snapshots). EVERY record's `prov.source="simulation"`
— this is scenario/demo data, NOT a real feed. In production this driver is replaced by a fusion of CCTV
people-counting, bus GPS and telco density writing the SAME schema. Grounded in the real 2015 Nashik
Simhastha (~2.5M on the peak Shahi Snan day; acute pressure at the narrow Ramkund ghat 13:00–14:00).

Certified path: writes go through the broker (/v1/write, X-Run-Token); the write-tenant comes from the
RUN context (a dev-token pins it to the developer sandbox; an installed run to the install tenant) — the
same code tests in dev and runs in prod unchanged. Scenario via the SCENARIO env (baseline|ramkund-crush).
"""
import json, math, os, urllib.request
import h3

API   = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
RES   = 9                                        # crowd cells at H3 res-9 (matches the Nashik baseline)
DATE  = "2027-09-11"                             # a plausible Amrit Snan day
FRAMES = [f"{DATE}T{h:02d}:00:00Z" for h in range(0, 19)]   # hourly 00:00 → 18:00
SCENARIO = os.environ.get("SCENARIO", "ramkund-crush")      # baseline | ramkund-crush

# The two real bathing hotspots (holy-dip sites), each with a crowd "blob".
HOTSPOTS = {
    "Ramkund (Nashik)":      {"lat": 19.9993, "lng": 73.7897, "k": 5},   # the documented 1–2 PM pinch-point
    "Kushavarta (Trimbak.)": {"lat": 19.9320, "lng": 73.5310, "k": 4},
}

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
    T = context().get("tenant") or os.environ.get("WRITE_TENANT", "kmc")
    prov = {"source": "simulation", "simulator": "kumbh-crowd-sim@0.1", "scenario": SCENARIO,
            "note": "SIMULATED demo data — not a real feed"}
    peak_density = {"Ramkund (Nashik)": 7.6 if SCENARIO == "ramkund-crush" else 5.2,
                    "Kushavarta (Trimbak.)": 4.6}
    CELL_M2 = h3.cell_area(h3.latlng_to_cell(19.99, 73.7, RES), unit="m^2") * 0.068   # occupiable fraction (→ ~2.5M peak)

    written = 0; peak_total = 0
    for site, hs in HOTSPOTS.items():
        center = h3.latlng_to_cell(hs["lat"], hs["lng"], RES)
        for cell in h3.grid_disk(center, hs["k"]):
            ring = h3.grid_distance(center, cell)
            spatial = math.exp(-(ring ** 2) / (2 * (hs["k"] / 1.6) ** 2))
            lat, lng = h3.cell_to_latlng(cell)
            boundary = [[b[1], b[0]] for b in h3.cell_to_boundary(cell)]     # [lng,lat] ring for rendering
            for t in FRAMES:
                hour = int(t[11:13]); sig = 2.6 if SCENARIO == "baseline" else 2.2
                inten = time_curve(hour, 13, sig)
                if SCENARIO == "ramkund-crush" and site.startswith("Ramkund") and hour in (13, 14) and ring <= 1:
                    inten = min(1.0, inten * 1.35)                           # the sharp 13:00–14:00 pinch-point spike
                d = round(peak_density[site] * spatial * inten, 2)
                if d < 0.3:
                    continue                                                # skip near-empty cells/frames
                head = int(d * CELL_M2)
                if hour == 13:
                    peak_total += head
                oid = f"ds:{T}:cell/crowd_density/{cell}-{t[:13]}"
                obj = {"schema": "cell.crowd_density", "id": oid, "tenant": T, "layer": "L2",
                       "space": {"h3_id": cell, "centroid": [lng, lat], "boundary": boundary},
                       "observed_at": t, "density_sqm": d, "headcount": head, "capacity": round(CELL_M2 * 4),
                       "band": band(d), "method": "sim-gaussian", "site": site, "prov": prov}
                call("/v1/write", {"scope": "cell.crowd_density", "id": oid, "object": obj})
                written += 1
    print(f"kumbh-crowd-sim [{SCENARIO}]: wrote {written} cell.crowd_density rows to {T}; "
          f"~peak 13:00 headcount ≈ {peak_total:,}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
