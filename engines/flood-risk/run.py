#!/usr/bin/env python3
"""flood-risk engine (INT-0002 PREDICT): geo.terrain × a rainfall FORECAST → risk.flood per cell.

Confined — its only I/O is the broker. It reads the real terrain (elevation, slope) for the AOI and a
rainfall forecast (RAINFALL_MM, a met scenario — this is a PREDICTION, not an observation; risk.flood is
explicitly a predicted ponding-risk), and writes a risk.flood score (0..1) + level per cell. Every record
carries method="model", confidence, and its drivers (elevation/slope/rainfall) so the prediction is
transparent and re-derivable. NO fabricated observations — the rainfall is a labelled forecast input.
"""
import json, os, time, urllib.request, urllib.error
from datetime import datetime, timezone

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
T = os.environ.get("WRITE_TENANT", "kmc")
RAINFALL_MM = float(os.environ.get("RAINFALL_MM", "125"))     # a labelled rainfall FORECAST (24h scenario)
CID = "ds:airawat:engine/flood-risk"; VER = "0.1.0"


def _call(path, body, timeout=60):
    for a in range(4):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if e.code >= 500 and a < 3:
                time.sleep(1.5 * (a + 1)); continue
            raise
        except urllib.error.URLError:
            if a < 3:
                time.sleep(1.5 * (a + 1)); continue
            raise


def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cells = [c.get("object") or c for c in _call("/v1/query", {"scope": "geo.terrain", "limit": 20000}).get("results", [])]
    cells = [c for c in cells if (c.get("space") or {}).get("boundary")]
    if not cells:
        print("no geo.terrain cells (run the terrain driver first) — nothing to predict."); return 0
    elevs = [c.get("elevation_m", 0) for c in cells] or [0]
    emin, emax = min(elevs), max(elevs)
    rain_factor = min(RAINFALL_MM / 150.0, 1.0)
    n = 0; levels = {"low": 0, "moderate": 0, "high": 0, "severe": 0}
    for c in cells:
        elev = c.get("elevation_m", 0.0); slope = c.get("slope_deg", 0.0)
        elev_norm = (elev - emin) / ((emax - emin) or 1)
        slope_factor = 1 - min(slope / 15.0, 1.0)
        suscept = 0.62 * (1 - elev_norm) + 0.38 * slope_factor          # terrain susceptibility 0..1
        score = round(suscept * (0.55 + 0.45 * rain_factor), 3)         # rainfall forecast scales it
        level = "severe" if score >= 0.85 else "high" if score >= 0.70 else "moderate" if score >= 0.50 else "low"
        levels[level] += 1
        h3 = (c.get("space") or {}).get("h3", "") or c.get("h3_id", "")
        rf = {"schema": "risk.flood", "id": f"ds:{T}:risk/flood/{h3}", "space": c["space"], "h3_id": h3,
              "score": score, "level": level, "method": "model", "confidence": 0.72,
              "drivers": {"elevation_m": elev, "slope_deg": slope, "rainfall_mm": RAINFALL_MM},
              "forecast": {"kind": "rainfall-24h", "rainfall_mm": RAINFALL_MM, "scenario": True},
              "observed_at": now, "tenant": T}
        _call("/v1/write", {"scope": "risk.flood", "id": rf["id"], "object": rf,
                            "valid_time": {"from": now}, "asserted_at": now})
        n += 1
    print(f"flood-risk: predicted risk.flood for {n} cells (rainfall forecast {RAINFALL_MM}mm/24h) — "
          f"levels {levels}. method=model; forecast-driven prediction, not an observation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
