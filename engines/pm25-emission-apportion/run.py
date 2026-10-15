#!/usr/bin/env python3
"""PM2.5 SOURCE APPORTIONMENT — Slice 2: BOTTOM-UP EMISSION INVENTORY. ADR-0073 follow-on.

Answers "what is CAUSING the PM2.5 here?" from PHYSICAL emission potential, not model correlation:

  1. For each H3 cell, compute an EMISSION POTENTIAL per source = activity proxy × emission factor:
       traffic       = road_length × road-class weight × congestion(junctions) × EF.traffic
       residential   = population × EF.residential            (cooking/heating fuel)
       industry      = nightlight_radiance × EF.industry      (+ commercial / DG combustion)
       construction  = built_area_m2 × EF.construction
       dust          = road_length × EF.road_dust × dryness   (resuspended road dust)
       biomass       = (needs cell.fire — ~0 until that layer is joined; kept for completeness)
  2. Split the CONCENTRATION honestly (emission ≠ concentration):
       PM2.5(cell) = regional_background (cell.cams pm25)  +  local_enhancement
     Only the local enhancement is apportioned across local sources by their emission SHARES.
  3. Write air.source_apportionment (per cell: fractions + dominant + the raw emission potentials) and an
     air.source_mix city rollup — the SAME schemas the SHAP engine uses, method="bottom-up-inventory".

Emission factors are GOVERNED, CITABLE config: ref.emission_factor records override the built-in Delhi priors,
so an air steward tunes them with no code change. The priors here are UNCALIBRATED — Slice 2.5 constrains them
with low-cost sensor observations (inverse calibration). No synthetic fill: a cell with no proxy data and no
local enhancement is written as 100% regional, not guessed.
"""
import json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
REGIONAL_SOURCE = os.environ.get("REGIONAL_SOURCE", "cell.cams").strip()   # per-cell regional background term
MIN_SHARE = float(os.environ.get("MIN_SHARE", "0.0"))
WRITE_MIX = os.environ.get("WRITE_MIX", "1").strip() in ("1", "true", "yes")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
OUT_SCHEMA = "air.source_apportionment"; MIX_SCHEMA = "air.source_mix"

# ── emission factors: built-in Delhi PRIORS (relative; only ratios across sources matter — output is normalised
#    to shares). Each is overridable by a ref.emission_factor record {source, value, basis, citation}. These are
#    UNCALIBRATED — Slice 2.5 solves them from low-cost sensor observations. Bases cite the activity rationale. ──
DEFAULT_EF = {
    "traffic":      {"value": 0.020,   "basis": "per metre road-equivalent × class × congestion (ARAI fleet PM2.5)"},
    "residential":  {"value": 0.050,   "basis": "per capita cooking/heating PM2.5 (TERI Delhi; LPG-dominant urban)"},
    "industry":     {"value": 1.00,    "basis": "per VIIRS nightlight radiance unit (combustion/DG proxy)"},
    "construction": {"value": 0.0003,  "basis": "per m² building footprint (construction/demolition dust)"},
    "road_dust":    {"value": 0.015,   "basis": "per metre road, dryness-scaled (resuspended road dust)"},
    "biomass_burning": {"value": 0.0,  "basis": "needs cell.fire activity — inactive until that layer is joined"},
}
CLASS_W = {"motorway": 3.0, "trunk": 3.0, "primary": 2.5, "secondary": 2.0, "tertiary": 1.3,
           "residential": 1.0, "service": 0.6, "unclassified": 1.0}
SOURCE_CATS = ["traffic", "industry", "construction_dust", "dust", "residential", "secondary",
               "biomass_burning", "regional_background"]


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def _rows(scope, filt=None, sort=None, limit=10000):
    """Paginated broker read. Static proxy scopes return one live row per cell; TIME-SERIES scopes (air.quality)
    MUST be filtered to a single observed_at or they return every hour's history (tens of thousands of rows)."""
    out, after = [], None
    while True:
        body = {"scope": scope, "limit": limit}
        if filt: body["filter"] = filt
        if sort: body["sort"] = sort
        if after: body["after"] = after
        r = call("/v1/query", body)
        res = r.get("results") or []
        out += [(it.get("object") or it) for it in res]
        if len(res) < limit:
            break
        after = r.get("next_cursor") or (res[-1].get("object") or res[-1]).get("id")
        if not after:
            break
    return out


def _latest_obs(scope):
    r = call("/v1/query", {"scope": scope, "sort": [{"field": "observed_at", "dir": "desc"}], "limit": 1}).get("results") or []
    return ((r[0].get("object") or r[0]).get("observed_at")) if r else None


def _h3(o):
    for path in (("space", "h3"), ("cell_ref", "h3_id")):
        v = o
        for k in path:
            v = v.get(k) if isinstance(v, dict) else None
        if v:
            return v
    return o.get("h3_id") or o.get("subject") or o.get("h3")


def _num(v, d=0.0):
    try:
        return float(v) if v is not None and v != "" else d
    except Exception:
        return d


def _by_cell(scope, filt=None):
    out = {}
    for o in _rows(scope, filt):
        h = _h3(o)
        if h:
            out[h] = o
    return out


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — an engine runs in exactly one tenant.")

    # emission factors: start from priors, overlay any governed ref.emission_factor records
    EF = {k: dict(v) for k, v in DEFAULT_EF.items()}
    for r in _rows("ref.emission_factor"):
        s = r.get("source") or r.get("id", "").rsplit("/", 1)[-1]
        if s in EF and r.get("value") is not None:
            EF[s]["value"] = _num(r.get("value"), EF[s]["value"]); EF[s]["basis"] = r.get("basis", EF[s]["basis"])
    ef = {k: EF[k]["value"] for k in EF}

    # air.quality is APPENDED per hour (bitemporal history) — read ONLY the latest hour, not all ~72k rows.
    obs = _latest_obs("air.quality") or NOW
    aq = _by_cell("air.quality", {"observed_at": obs})
    if not aq:
        raise SystemExit("FATAL: no air.quality at latest hour — run the pm25-model nowcast first.")
    # static proxy layers: one live row per cell (~24k each), paginated. cell.cams is NOT read (regional = floor).
    bld = _by_cell("cell.buildings"); pop = _by_cell("demographics.population"); rds = _by_cell("cell.roads")
    nl = _by_cell("econ.nightlight"); rain = _by_cell("cell.rainfall")
    # REGIONAL BACKGROUND = the transboundary FLOOR present across the whole area — a low percentile of the
    # modelled field (NOT the per-cell CAMS value: CAMS is a coarse 40km regional model that reads as high as
    # the local nowcast, so using it per-cell absorbs the entire signal into "regional"). cell.cams is read only
    # as an optional sanity bound. local_enhancement = pm − floor is what gets apportioned to local sources.
    REGIONAL_PCT = float(os.environ.get("REGIONAL_PCT", "0.10"))
    pm_vals = sorted(_num(o.get("pm25")) for o in aq.values() if _num(o.get("pm25")) > 0)
    floor = pm_vals[max(0, int(REGIONAL_PCT * len(pm_vals)) - 1)] if pm_vals else 0.0

    items = []
    tot = {c: 0.0 for c in SOURCE_CATS}                      # concentration-weighted citywide totals (µg·cells)
    dom_count = {}
    for h, a in aq.items():
        pm = _num(a.get("pm25"))
        if pm <= 0:
            continue
        reg = min(floor, pm)                                # transboundary floor, present everywhere
        local = max(pm - reg, 0.0)                          # the local enhancement we apportion

        b = bld.get(h) or {}; rd = rds.get(h) or {}
        road_m = _num(rd.get("road_length_m")); seg = _num(rd.get("segment_count"))
        road_km = max(road_m / 1000.0, 0.05)
        cls = CLASS_W.get((rd.get("primary_class") or "").lower(), 1.0)
        congestion = 1.0 + 0.5 * min((seg / road_km) / 20.0, 1.0) if road_m > 0 else 1.0
        people = _num((pop.get(h) or {}).get("population"))
        rad = _num((nl.get(h) or {}).get("avg_radiance"))
        barea = _num(b.get("built_area_m2"))
        rmm = _num((rain.get(h) or {}).get("rainfall_mm"))
        dry = 1.0 / (1.0 + rmm)                              # dry → more resuspended dust

        E = {
            "traffic":          road_m * cls * congestion * ef["traffic"],
            "residential":      people * ef["residential"],
            "industry":         rad * ef["industry"],
            "construction_dust": barea * ef["construction"],
            "dust":             road_m * ef["road_dust"] * (0.5 + 0.5 * dry),
            "biomass_burning":  0.0 * ef["biomass_burning"],
            "secondary":        0.0,                          # no bottom-up precursor chemistry in Slice 2
        }
        Esum = sum(E.values())
        frac = {c: 0.0 for c in SOURCE_CATS}
        if local > 0 and Esum > 0:
            for c, v in E.items():
                frac[c] = round(v / Esum, 4)
        # regional carried as its own share of the TOTAL concentration (regional + local)
        frac["regional_background"] = round(reg / pm, 4) if pm > 0 else 1.0
        # rescale local-source shares to (1 - regional) so the whole cell sums to ~1
        loc_share = 1.0 - frac["regional_background"]
        if Esum > 0 and loc_share > 0:
            for c in E:
                frac[c] = round(frac[c] * loc_share, 4)
        if MIN_SHARE > 0:
            frac = {c: (v if v >= MIN_SHARE else 0.0) for c, v in frac.items()}
        dominant = max(frac, key=frac.get) if any(frac.values()) else "regional_background"

        sp = a.get("space") or {}
        oid = f"ds:{TENANT}:air/source_apportionment/{h}"
        obj = {"schema": OUT_SCHEMA, "id": oid, "observed_at": obs, "pm25": round(pm, 1),
               "regional_pm25": round(reg, 1), "local_pm25": round(local, 1),
               "dominant_source": dominant, "fractions": frac,
               "emission_potential": {c: round(E[c], 2) for c in E},
               "space": {"h3": h, "res": int(sp.get("res", 9)) if str(sp.get("res", "")).isdigit() else 9,
                         "centroid": sp.get("centroid")},
               "method": "bottom-up-inventory", "confidence": 0.5,
               "prov": {"method": "bottom-up-inventory", "component": "ds:airawatdeveloper:engine/pm25-emission-apportion@0.1.0",
                        "regional_source": REGIONAL_SOURCE, "ef": {k: ef[k] for k in ef},
                        "note": "emission-potential apportionment of local enhancement; EFs are uncalibrated Delhi priors (pending low-cost-sensor calibration, Slice 2.5)",
                        "observed_at": obs}}
        obj["space"] = {k: v for k, v in obj["space"].items() if v is not None}
        items.append({"scope": OUT_SCHEMA, "id": oid, "object": obj, "valid_time": {"from": obs}})
        # citywide concentration-weighted accumulation (µg contribution per source)
        tot["regional_background"] += reg
        if local > 0 and Esum > 0:
            for c in E:
                tot[c] += local * (E[c] / Esum)
        if dominant:
            dom_count[dominant] = dom_count.get(dominant, 0) + 1

    written = errs = 0
    for k in range(0, len(items), 2000):
        r = call("/v1/write/batch", {"items": items[k:k + 2000], "asserted_at": NOW})
        written += r.get("written", 0); errs += r.get("error_count", 0)
        if r.get("errors"):
            print(f"  batch {k}: sample errors {r['errors'][:2]}", flush=True)

    mix = None
    gtot = sum(tot.values())
    if WRITE_MIX and gtot > 0:
        cityf = {c: round(tot[c] / gtot, 4) for c in SOURCE_CATS}
        mid = f"ds:{TENANT}:air/source_mix/latest"
        mix = {"schema": MIX_SCHEMA, "id": mid, "observed_at": obs, "n_cells": len(items),
               "fractions": cityf, "dominant_source": max(cityf, key=cityf.get),
               "dominant_cell_counts": dom_count, "method": "bottom-up-inventory", "confidence": 0.5,
               "prov": {"component": "ds:airawatdeveloper:engine/pm25-emission-apportion@0.1.0",
                        "weighting": "concentration (µg) weighted citywide", "ef": {k: ef[k] for k in ef}, "observed_at": obs}}
        call("/v1/write", {"scope": MIX_SCHEMA, "id": mid, "object": mix, "valid_time": {"from": obs}, "asserted_at": NOW})

    print(f"EMISSION-APPORTION wrote {written} air.source_apportionment cells (errors {errs}) @ {obs}", flush=True)
    if mix:
        print(f"  city mix (conc-weighted): {json.dumps(mix['fractions'])} · dominant={mix['dominant_source']}", flush=True)
    print(f"RESULT: {json.dumps({'apportioned': True, 'method': 'bottom-up-inventory', 'cells_written': written, 'errors': errs, 'observed_at': obs, 'city_mix': (mix or {}).get('fractions')})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
