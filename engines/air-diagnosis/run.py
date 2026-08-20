#!/usr/bin/env python3
"""Air-Quality Diagnosis Engine (INT-0001, kind=engine). Confined: its ONLY I/O is the kernel broker.
Reads a pm2.5 station reading → produces a DESCRIPTIVE Analysis (current AQI) and, if unhealthy, a
PRESCRIPTIVE Analysis + a Signal, and offers a Recommendation (advisory — governance decides).

Env: AIRAWAT_API (kernel), RUN_TOKEN (the engine's minted token), STATION (the fact id to read).
The tiny HTTP client here graduates to the `sdk/` repo (broker/HTTP binding).
"""
import json, os, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
STATION = os.environ.get("STATION", "ds:kmc:assertion/air/station-7/today")
WHO_24H = 15.0  # µg/m³ WHO 2021 24-h guideline (illustrative)

def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

def aqi_band(v):
    return "good" if v <= 30 else "moderate" if v <= 60 else "poor" if v <= 90 else "very poor"

def main():
    ctx = _call("/v1/context", {}) if False else json.loads(urllib.request.urlopen(
        urllib.request.Request(API + "/v1/context", headers={"X-Run-Token": TOKEN})).read())
    print(f"engine run: {ctx['component_id']}  caps={[c['verb']+':'+c['scope'] for c in ctx['effective_capabilities']]}")

    fact = _call("/v1/read", {"scope": "air.sensor_reading", "id": STATION})["object"]
    if not fact:
        print(f"no fact at {STATION}"); return 1
    pm25 = float(fact["pm25"]); band = aqi_band(pm25)
    method = {"ref": {"id": "ds:kmc:method/aqi-band", "version": "1.0"}}
    unc = {"id": f"ds:kmc:uncertainty/air-diag/{STATION.rsplit('/',1)[-1]}"}
    base = STATION.rsplit("/", 1)[-1]

    # DESCRIPTIVE: what is the AQI now
    desc = {"conforms_to": {"schema": "https://airawat.org/intelligence/0001/analysis.schema.json", "version": "0.1.0"},
            "id": f"ds:kmc:analysis/air/{base}/descriptive", "analysis_type": "descriptive",
            "produced_by": {"component": {"id": ctx["component_id"], "version": "0.1.0"}},
            "method": method, "inputs": [{"id": STATION, "version": "1"}],
            "output": {"pm25": pm25, "band": band}, "uncertainty_ref": unc, "produced_at": "2025-05-12T06:10:00Z"}
    _call("/v1/write", {"scope": "intelligence.analysis", "id": desc["id"], "object": desc})
    print(f"  descriptive → pm25={pm25} band={band}")

    recommendation = None
    if pm25 > 60:
        # PRESCRIPTIVE: what to do
        presc = {**desc, "id": f"ds:kmc:analysis/air/{base}/prescriptive", "analysis_type": "prescriptive",
                 "built_on": [{"id": desc["id"]}],
                 "output": {"action": "issue-public-advisory", "reason": f"pm25 {pm25} in '{band}' band"}}
        _call("/v1/write", {"scope": "intelligence.analysis", "id": presc["id"], "object": presc})
        # SIGNAL (a nudge — resolve the source before acting)
        sig = {"conforms_to": {"schema": "https://airawat.org/intelligence/0001/signal.schema.json", "version": "0.1.0"},
               "id": f"ds:kmc:signal/air/{base}", "signal_type": "air-quality-alert",
               "raised_by": {"id": presc["id"]}, "subject_ref": {"id": STATION}, "severity": band,
               "delivery_semantics": "at-least-once", "detected_at": "2025-05-12T06:11:00Z"}
        _call("/v1/write", {"scope": "intelligence.signal", "id": sig["id"], "object": sig})
        # RECOMMENDATION (advisory — the engine offers, an Authority decides)
        recommendation = {"recommends": {"outcome": "issue-public-advisory"}, "value": pm25, "band": band,
                          "prescriptive_analysis": presc["id"]}
        print(f"  prescriptive → advisory | signal raised | RECOMMENDATION offered (advisory)")
    print("RESULT:", json.dumps({"pm25": pm25, "band": band, "recommendation": recommendation}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
