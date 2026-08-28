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
    # STEP 0 (data quality): the DEM returns 0 m over water / no-data (a coastal city like Mumbai is islands +
    # creeks), and the susceptibility model reads LOW elevation as HIGH risk — so unmasked, every water/no-data
    # cell scores "severe" and the whole map goes red. Score only LAND cells (elevation > NODATA_M) and normalise
    # over the real land range, so the layer discriminates genuinely low-lying land instead of the sea. Water/
    # no-data cells get NO risk.flood record (correct — they are not floodable land).
    NODATA_M = float(os.environ.get("NODATA_M", "0"))
    land = [c for c in cells if isinstance(c.get("elevation_m"), (int, float)) and c.get("elevation_m") > NODATA_M]
    skipped = len(cells) - len(land)
    if not land:
        print(f"no LAND cells — all {len(cells)} at/below {NODATA_M}m (water/no-data). Nothing to predict."); return 0
    # PERCENTILE-RANK normalisation, not min-max: a coastal city is nearly all low-lying with a few hills, so
    # min-max compresses every low cell to ~0 (→ all "high"). Rank-normalising spreads susceptibility evenly, so
    # the layer RANKS relative low-lying-ness (the lowest cells = highest risk) — a meaningful screening index.
    import bisect
    elevs = sorted(c["elevation_m"] for c in land)
    emin, emax = elevs[0], elevs[-1]; _dn = (len(elevs) - 1) or 1
    rain_factor = min(RAINFALL_MM / 150.0, 1.0)
    # T2 (flood-prediction.md): DRAINAGE RELIEF. cell.drainage carries km-of-drain per km² per cell (osm-drainage).
    # Dense, connected drainage conveys stormwater, so a DRAINED low cell ponds less than an UNDRAINED one — cut its
    # susceptibility in proportion to drain density (capped). Cells with NO drainage data stay pure T1 (we can't tell
    # "undrained" from "unmapped", so we don't penalise them). Each row records method.tier = T2 (relieved) | T1.
    DRAIN_W = float(os.environ.get("DRAIN_WEIGHT", "0.4"))          # max fraction drainage can cut susceptibility
    DRAIN_REF = float(os.environ.get("DRAIN_REF_KM_KM2", "6"))      # drain density (km/km²) treated as "well-drained"
    drainage = {}
    for _d in (_call("/v1/query", {"scope": "cell.drainage", "limit": 20000}).get("results") or []):
        _o = _d.get("object") or _d; _h = (_o.get("cell_ref") or {}).get("h3_id"); _dd = _o.get("drain_density_km_per_km2")
        if _h and isinstance(_dd, (int, float)):
            drainage[_h] = _dd
    n = 0; t2 = 0; levels = {"low": 0, "moderate": 0, "high": 0, "severe": 0}
    for c in land:
        elev = c.get("elevation_m", 0.0); slope = c.get("slope_deg", 0.0)
        h3 = (c.get("space") or {}).get("h3", "") or c.get("h3_id", "")
        elev_norm = bisect.bisect_left(elevs, elev) / _dn              # 0 = lowest land, 1 = highest
        slope_factor = 1 - min(slope / 15.0, 1.0)
        suscept = 0.62 * (1 - elev_norm) + 0.38 * slope_factor          # T1 terrain susceptibility 0..1
        dd = drainage.get(h3); tier = "T1"                              # T2 drainage relief where drainage is mapped
        if dd is not None:
            suscept *= (1 - DRAIN_W * min(dd / DRAIN_REF, 1.0)); tier = "T2"; t2 += 1
        score = round(suscept * (0.55 + 0.45 * rain_factor), 3)         # rainfall forecast scales it
        level = "severe" if score >= 0.85 else "high" if score >= 0.70 else "moderate" if score >= 0.50 else "low"
        levels[level] += 1
        rf = {"schema": "risk.flood", "id": f"ds:{T}:risk/flood/{h3}", "space": c["space"], "h3_id": h3,
              "score": score, "level": level, "method": "model", "confidence": 0.72,
              "drivers": {"elevation_m": elev, "slope_deg": slope, "rainfall_mm": RAINFALL_MM,
                          "drain_density_km_per_km2": dd, "tier": tier},
              # `forecast` is a resolvable ref to the met.forecast used; here a LABELLED scenario string (no
              # met.forecast record — this is a T1 scenario input, honestly flagged as such).
              "forecast": f"scenario:rainfall-24h:{RAINFALL_MM}mm",
              "prov": {"method": "model", "derived_from_refs": [f"ds:{T}:geo/terrain/{h3}"],
                       "forecast_scenario": f"rainfall-24h:{RAINFALL_MM}mm", "component": f"{CID}@{VER}",
                       "observed_at": now},
              "observed_at": now, "tenant": T}
        _call("/v1/write", {"scope": "risk.flood", "id": rf["id"], "object": rf,
                            "valid_time": {"from": now}, "asserted_at": now})
        n += 1
    print(f"flood-risk: predicted risk.flood for {n} LAND cells (masked {skipped} water/no-data at/below {NODATA_M}m; "
          f"{t2} cells got T2 drainage relief, {n - t2} pure T1; rainfall {RAINFALL_MM}mm/24h; land elevation "
          f"{emin}-{emax}m) — levels {levels}. method=model; forecast-driven prediction, not an observation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
