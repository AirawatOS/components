#!/usr/bin/env python3
"""City Intelligence — the City Scan Analyst, a certified in-process engine (verb: city-ask).

Grounded Q&A over the tenant's GOVERNED layers. It reads the live records via its own read grants (ctx["kfind"],
attributed component://…), computes the FACTS in code — the model never computes or invents a figure — grounds the
confined model (ctx["ai_complete"]) on ONLY those facts, and returns {answer, sources, facts}.

0.4.0 — a general city analyst, not only the mass-gathering one:
  • CITY LAYERS: the per-cell city.cell_profile rollup (engine/city-profile: heat, rainfall, forecast, built-up/green,
    hydrology, population, buildings, roads, facilities). If no profile exists yet it falls back to the raw cell layers
    (slower) and says so.
  • PUBLIC WORKS: works.city_summary, works.ward_rollup, works.contractor_rollup (small rollups — never the raw tables).
  • ALL open governance.recommendation records (not only the flood/crowd ones), and city.coverage for evidence quality.
  • The facts are chosen by the question's TOPIC (heat, flood, services, green, access/congestion, buildings, works…);
    every answer carries which layers are PRESENT and which are MISSING, and a proxy is always labelled as a proxy
    (e.g. no traffic layer → congestion is answered only by road density × population, said so).
  • The mass-gathering facts (risk.flood × cell.crowd_density × vision.detection) are kept, unchanged, whenever those
    layers exist.
AI informs; a human decides (ADR-0029).
"""
import json, math, os, re

HOT_FLOOD = ("high", "severe")
TOP = 8
ANSWER_TOKENS = 1800   # 0.4.1: a reasoning model spends tokens thinking first; 900 left nothing for the answer (finish=length, empty)

# raw-layer fallback when no city.cell_profile exists: schema → {profile field: source field}
RAW = {
    "cell.heat": {"heat_c": "value"}, "cell.rainfall": {"rainfall_mm": "rainfall_mm"},
    "cell.weather_forecast": {"forecast_peak_precip_mm": "peak_precip_mm", "forecast_total_precip_mm": "total_precip_mm"},
    "cell.builtup": {"builtup_frac": "value"}, "cell.greencover": {"green_frac": "value"},
    "cell.hydrology": {"flood_susceptibility": "flood_susceptibility", "hand_mean_m": "hand_mean_m"},
    "demographics.population": {"population": "population", "density_per_km2": "density_per_km2"},
    "cell.buildings": {"building_coverage": "building_coverage", "mean_height_m": "mean_height_m", "built_volume_m3": "built_volume_m3"},
    "cell.roads": {"road_density_km_per_km2": "road_density_km_per_km2", "road_length_m": "road_length_m"},
}
# profile field → the layer it comes from (for present / missing reporting)
FIELD_LAYER = {f: s for s, m in RAW.items() for f in m}
FIELD_LAYER["facility_count"] = "geo.facility"
# 0.4.6 — mobility layers (driver/mobility-indexes), joined onto the cells by h3 whenever a mobility question is asked
MOBILITY = {
    "mobility.walkability": {"walkability_score": "walkability_score", "intersection_density_per_km2": "intersection_density_per_km2",
                             "street_density_km_per_km2": "street_density_km_per_km2", "dead_end_share": "dead_end_share"},
    "mobility.transit_access": {"transit_gap_score": "transit_gap_score", "dist_bus_stop_m": "dist_bus_stop_m",
                                "dist_metro_m": "dist_metro_m"},
    "mobility.chokepoint": {"chokepoint_score": "chokepoint_score"},
}
FIELD_LAYER.update({f: s for s, m in MOBILITY.items() for f in m})

TOPICS = {   # topic → (question keywords, profile fields that answer it)
    "heat":      (("heat", "hot", "temperature", "cool"), ("heat_c", "population")),
    "flood":     (("flood", "water", "drain", "rain", "monsoon", "inundat", "storm"),
                  ("flood_susceptibility", "rainfall_mm", "forecast_peak_precip_mm", "population")),
    "services":  (("health", "hospital", "clinic", "school", "facility", "facilities", "service", "amenit", "centre", "center"),
                  ("facility_count", "population")),
    "green":     (("green", "tree", "park", "open space", "vegetation", "canopy"), ("green_frac", "builtup_frac", "population")),
    "access":    (("road", "traffic", "congest", "transport", "mobility", "access", "connect", "bottleneck", "chokepoint"),
                  ("road_density_km_per_km2", "density_per_km2", "chokepoint_score")),
    "transit":   (("bus", "metro", "transit", "public transport", "stop", "commut", "station", "last mile", "last-mile"),
                  ("transit_gap_score", "dist_bus_stop_m", "dist_metro_m", "population")),
    "walk":      (("walk", "pedestrian", "footpath", "sidewalk", "street grid", "cycl", "15-minute", "15 minute"),
                  ("walkability_score", "intersection_density_per_km2", "population")),
    "buildings": (("building", "housing", "construct", "dense", "density", "height", "built"),
                  ("building_coverage", "built_volume_m3", "density_per_km2")),
    "population": (("population", "people", "resident", "crowd", "where do people"), ("population", "density_per_km2")),
    "works":     (("works", "spend", "spent", "contract", "tender", "ward", "budget", "payment", "project", "procure"), ()),
}
CATEGORY_WORDS = {"health": ("health", "hospital", "clinic"), "education": ("school", "education", "college"),
                  "emergency": ("fire", "police", "emergency")}


def _h3(o):
    g = o.get("space") or o.get("cell_ref") or {}
    return g.get("h3_id") or g.get("h3") or o.get("h3_id")


def _get(o, path):
    for k in path.split("."):
        o = o.get(k) if isinstance(o, dict) else None
    return o


def _age_minutes(iso):
    import time, calendar
    try:
        return round((time.time() - calendar.timegm(time.strptime((iso or "")[:19], "%Y-%m-%dT%H:%M:%S"))) / 60.0)
    except Exception:
        return None


def evidence_quality(facts_by_schema, coverage):
    """Per source layer: records, coverage %, freshest/oldest observation, observed-vs-inferred, mean confidence."""
    out = {}
    for schema, rows in facts_by_schema.items():
        ages = [a for a in (_age_minutes(r.get("observed_at") or "") for r in rows) if a is not None]
        methods, confs = {}, []
        for r in rows:
            m = (r.get("method") or "").lower()
            k = ("observed" if m in ("measured", "sensor", "human", "vision-count", "person-detection", "count")
                 else "inferred" if (m in ("model", "statistical", "rule", "sim-gaussian", "simulation") or m.startswith("sim"))
                 else "unattributed")
            methods[k] = methods.get(k, 0) + 1
            c = r.get("confidence")
            if isinstance(c, (int, float)):
                confs.append(c)
        cv = coverage.get(schema) or {}
        out[schema] = {"records": len(rows),
                       "coverage_pct": cv.get("coverage_pct") or cv.get("coverage") or cv.get("coverage_percent"),
                       "gaps": cv.get("gaps"),
                       "freshness_min": (min(ages) if ages else None), "staleness_min": (max(ages) if ages else None),
                       "observed_vs_inferred": methods,
                       "mean_confidence": (round(sum(confs) / len(confs), 3) if confs else cv.get("mean_confidence"))}
    return out


# ── the mass-gathering facts (0.3.0 behaviour, kept) ────────────────────────────────────────────────────────────────
def crowd_facts(kfind, cov):
    flood = {}
    for o in kfind("risk.flood"):
        h = _h3(o)
        if h and o.get("level") in HOT_FLOOD:
            flood[h] = o
    crowd = {}
    for o in kfind("cell.crowd_density"):
        h = _h3(o)
        if h and (h not in crowd or (o.get("headcount") or 0) > (crowd[h].get("headcount") or 0)):
            crowd[h] = o
    cctv = {}
    for o in kfind("vision.detection"):
        h = _h3(o)
        if h and (h not in cctv or (o.get("observed_at") or "") > (cctv[h].get("observed_at") or "")):
            cctv[h] = o
    if not (flood or crowd or cctv):
        return None, []
    overlap = []
    for h, f in flood.items():
        c = crowd.get(h)
        if not c:
            continue
        if (c.get("band") or "").lower() in ("normal", "") and (c.get("density_sqm") or 0) < 3.0:
            continue
        overlap.append({"h3": h, "site": c.get("site") or "site", "flood_level": f.get("level"), "flood_score": f.get("score"),
                        "headcount": c.get("headcount"), "density": c.get("density_sqm"), "band": c.get("band"),
                        "cctv": (cctv.get(h) or {}).get("count")})
    overlap.sort(key=lambda x: -(x["flood_score"] or 0))
    facts = {"flood_cells_high_or_severe": len(flood), "crowd_cells": len(crowd), "cctv_hotspot_cells": len(cctv),
             "flood_and_crowd_overlap_cells": len(overlap),
             "people_exposed_in_overlap": sum(x["headcount"] or 0 for x in overlap), "top_overlaps": overlap[:TOP],
             "evidence_quality": evidence_quality({"risk.flood": list(flood.values()), "cell.crowd_density": list(crowd.values()),
                                                   "vision.detection": list(cctv.values())}, cov)}
    used = [s for s, d in (("risk.flood", flood), ("cell.crowd_density", crowd), ("vision.detection", cctv)) if d]
    return facts, used


# ── the city layers: the cell profile (or the raw layers as a fallback) ────────────────────────────────────────────
def join_mobility(kfind, cells):
    """Add the mobility fields onto the cells (by h3; a cell missing from the base set is added). → {schema: n}."""
    counts = {}
    for schema, fmap in MOBILITY.items():
        n = 0
        try:
            rows = kfind(schema)
        except Exception:                      # e.g. this engine's grant predates the mobility reads → reported missing
            rows = []
        for o in rows:
            h = _h3(o)
            if not h:
                continue
            c = cells.get(h)
            if c is None:
                c = cells[h] = {"cell_ref": {"h3_id": h, "centroid": (o.get("cell_ref") or {}).get("centroid")}}
            for dst, src in fmap.items():
                v = _get(o, src)
                if v is not None:
                    c[dst] = v
            n += 1
        if n:
            counts[schema] = n
    return counts


def city_cells(kfind):
    """(cells {h3: profile-shaped dict}, layer_counts {schema: n}, source) — the rollup if present, else the raw join."""
    prof = kfind("city.cell_profile")
    if prof:
        counts = {}
        for p in prof:
            for s in (p.get("layers_present") or []):
                counts[s] = counts.get(s, 0) + 1
        return {_h3(p): p for p in prof if _h3(p)}, counts, "city.cell_profile"
    cells, counts = {}, {}
    for schema, fmap in RAW.items():
        n = 0
        for o in kfind(schema):
            h = _h3(o)
            if not h:
                continue
            c = cells.setdefault(h, {"cell_ref": {"h3_id": h, "centroid": (o.get("cell_ref") or o.get("space") or {}).get("centroid")}})
            for dst, src in fmap.items():
                v = _get(o, src)
                if v is not None:
                    c[dst] = v
            n += 1
        counts[schema] = n
    n = 0
    for o in kfind("geo.facility"):
        h = _h3(o)
        if not h:
            continue
        c = cells.setdefault(h, {"cell_ref": {"h3_id": h, "centroid": o.get("centroid")}})
        cat = o.get("category") or "other"
        c.setdefault("facilities", {})[cat] = c.get("facilities", {}).get(cat, 0) + 1
        c["facility_count"] = c.get("facility_count", 0) + 1
        n += 1
    counts["geo.facility"] = n
    return cells, {s: k for s, k in counts.items() if k}, ("raw layers (no city.cell_profile yet — run City Profile)" if cells else None)


# 0.4.5 — the GHSL population grid is a MODEL that can over-allocate dense commercial cores (staging: a Cottonpete cell at
# 197,759 /km², 1.7x the densest census ward). A cell denser than the densest 2011 census ward is not ranked on density;
# it is listed under excluded_as_implausible instead (never silently dropped).
DENSITY_MAX = float(os.environ.get("DENSITY_PLAUSIBLE_MAX", "113590"))


def plausible(c):
    d = c.get("density_per_km2")
    return not (isinstance(d, (int, float)) and d > DENSITY_MAX)


def _row(c, fields):
    cen = (c.get("cell_ref") or {}).get("centroid")
    r = {"h3": _h3(c), "centroid": cen}
    if isinstance(cen, (list, tuple)) and len(cen) >= 2 and all(isinstance(x, (int, float)) for x in cen[:2]):
        r["lat_lon"] = "%.4f, %.4f" % (cen[1], cen[0])   # 0.4.2: ready to print — the model never swaps [lon, lat] itself
    for f in fields:
        if c.get(f) is not None:
            r[f] = c[f]
    return r


def _top(cells, key, fields, n=TOP, reverse=True, cond=None):
    rows = [c for c in cells.values() if isinstance(key(c), (int, float)) and (cond is None or cond(c))]
    rows.sort(key=key, reverse=reverse)
    return [_row(c, fields) for c in rows[:n]], len(rows)


def _stats(cells, f):
    v = sorted(c[f] for c in cells.values() if isinstance(c.get(f), (int, float)))
    if not v:
        return None
    return {"cells": len(v), "min": v[0], "median": v[len(v) // 2], "max": v[-1]}


def topics_of(question):
    q = question.lower()
    t = [k for k, (words, _) in TOPICS.items() if any(w in q for w in words)]
    return t or ["overview"]


def topic_facts(topic, cells, question):
    f = {}
    num = lambda k: (lambda c: c.get(k) if isinstance(c.get(k), (int, float)) else None)
    if topic == "heat":
        f["heat_c"] = _stats(cells, "heat_c")
        f["hottest_cells"], _ = _top(cells, num("heat_c"), ("heat_c", "population"))
        f["heat_exposure_cells (heat × population)"], _ = _top(
            cells, lambda c: (c.get("heat_c") or 0) * (c.get("population") or 0), ("heat_c", "population"),
            cond=lambda c: c.get("heat_c") is not None and c.get("population") is not None)
    elif topic == "flood":
        f["flood_susceptibility"] = _stats(cells, "flood_susceptibility")
        f["most_flood_susceptible_cells"], _ = _top(cells, num("flood_susceptibility"), ("flood_susceptibility", "population", "rainfall_mm"))
        f["people_in_susceptible_cells (top by susceptibility × population)"], _ = _top(
            cells, lambda c: (c.get("flood_susceptibility") or 0) * (c.get("population") or 0), ("flood_susceptibility", "population"),
            cond=lambda c: c.get("flood_susceptibility") is not None and c.get("population") is not None)
        f["rainfall_mm (climate normal)"] = _stats(cells, "rainfall_mm")
        f["forecast_peak_precip_mm (next 48h)"] = _stats(cells, "forecast_peak_precip_mm")
        f["wettest_forecast_cells"], _ = _top(cells, num("forecast_peak_precip_mm"), ("forecast_peak_precip_mm", "population"))
    elif topic == "services":
        q = question.lower()
        cats = [c for c, words in CATEGORY_WORDS.items() if any(w in q for w in words)] or None
        has = (lambda c: any((c.get("facilities") or {}).get(k) for k in cats)) if cats else (lambda c: (c.get("facility_count") or 0) > 0)
        any_fac = any(c.get("facility_count") is not None for c in cells.values())
        f["facility_layer_present"] = any_fac
        f["category_asked"] = cats or "any"
        if any_fac:
            f["populous_cells_without_such_a_facility (proxy: within the cell only, not travel distance)"], f["cells_without"] = _top(
                cells, num("population"), ("population", "density_per_km2", "facility_count"),
                cond=lambda c: c.get("population") is not None and not has(c))
    elif topic == "green":
        f["green_frac"] = _stats(cells, "green_frac")
        f["least_green_populated_cells"], _ = _top(cells, num("green_frac"), ("green_frac", "builtup_frac", "population"),
                                                  reverse=False, cond=lambda c: c.get("population") is not None)
    elif topic == "access":
        f["traffic_or_congestion_layer_present"] = False
        f["road_density_km_per_km2"] = _stats(cells, "road_density_km_per_km2")
        # 0.4.2: only the DENSER HALF of the city competes (density ≥ the city median), and road density is floored at
        # 1 km/km² — otherwise near-empty outskirts with almost no roads (1,607/km² on 0.19 km/km²) topped the list.
        _dens = _stats(cells, "density_per_km2")
        _med = (_dens or {}).get("median") or 0
        f["PROXY_people_per_road_density_in_denser_half (NOT congestion — no traffic data)"], _ = _top(
            cells, lambda c: (c.get("density_per_km2") or 0) / max(c.get("road_density_km_per_km2") or 1.0, 1.0),
            ("density_per_km2", "road_density_km_per_km2"),
            cond=lambda c: c.get("density_per_km2") is not None and c.get("road_density_km_per_km2") is not None
            and c["density_per_km2"] >= _med and plausible(c))
        if any(isinstance(c.get("chokepoint_score"), (int, float)) for c in cells.values()):
            f["PROXY_network_chokepoints (routes concentrate here — sampled betweenness on the arterial network; NOT observed traffic)"], _ = _top(
                cells, num("chokepoint_score"), ("chokepoint_score",), cond=lambda c: isinstance(c.get("chokepoint_score"), (int, float)))
    elif topic == "transit":
        f["dist_bus_stop_m"] = _stats(cells, "dist_bus_stop_m")
        f["dist_metro_m"] = _stats(cells, "dist_metro_m")
        under = [c for c in cells.values() if isinstance(c.get("transit_gap_score"), (int, float)) and c["transit_gap_score"] > 0]
        f["underserved_cells"] = len(under)
        f["people_in_underserved_cells"] = round(sum(c.get("population") or 0 for c in under if plausible(c)))
        f["worst_transit_gaps (people far from a bus stop / metro; straight-line distance)"], _ = _top(
            cells, num("transit_gap_score"), ("transit_gap_score", "population", "dist_bus_stop_m", "dist_metro_m"),
            cond=lambda c: (c.get("transit_gap_score") or 0) > 0 and plausible(c))
    elif topic == "walk":
        f["walkability_score"] = _stats(cells, "walkability_score")
        f["least_walkable_populated_cells (street layout only)"], _ = _top(
            cells, num("walkability_score"), ("walkability_score", "intersection_density_per_km2", "population"), reverse=False,
            cond=lambda c: isinstance(c.get("walkability_score"), (int, float)) and (c.get("population") or 0) >= 2000 and plausible(c))
        f["most_walkable_cells"], _ = _top(cells, num("walkability_score"), ("walkability_score", "intersection_density_per_km2"))
    elif topic == "buildings":
        f["building_coverage"] = _stats(cells, "building_coverage")
        f["most_built_up_cells"], _ = _top(cells, num("built_volume_m3"), ("built_volume_m3", "building_coverage", "mean_height_m", "density_per_km2"))
    elif topic == "population":
        f["population"] = _stats(cells, "population")
        f["most_populous_cells"], _ = _top(cells, num("population"), ("population", "density_per_km2"), cond=plausible)
    elif topic == "overview":
        for k in ("heat_c", "rainfall_mm", "flood_susceptibility", "population", "green_frac", "building_coverage", "road_density_km_per_km2"):
            s = _stats(cells, k)
            if s:
                f[k] = s
    return {k: v for k, v in f.items() if v not in (None, [], {})}


PLACE_NEAR_KM, PLACE_MAX_KM = 1.0, 2.5   # ≤1 km → "Jayanagar (0.4 km)"; ≤2.5 km → "near Jayanagar (1.6 km)"; beyond → no name


def load_places(kfind):
    """The tenant's gazetteer (space.place, written by gazetteer-builder from OSM): named localities and wards as
    (lon, lat, name). Pincodes are not names, so they are left out. Empty when the gazetteer has not been built."""
    out = []
    for p in kfind("space.place"):
        c = p.get("centroid") or []
        if p.get("kind") in ("locality", "ward") and p.get("name") and len(c) >= 2:
            try:
                out.append((float(c[0]), float(c[1]), str(p["name"])))
            except (TypeError, ValueError):
                pass
    return out


def nearest_place(places, lon, lat):
    """'Name (0.4 km)' / 'near Name (1.6 km)' for the closest named place, or None if none is within PLACE_MAX_KM.
    Equirectangular distance — exact enough at city scale. The distance is always stated: a cell is NEAR a named
    place, it is not claimed to be inside it."""
    if not places or lon is None or lat is None:
        return None
    k = math.cos(math.radians(lat))
    best = min(places, key=lambda p: ((p[0] - lon) * k) ** 2 + (p[1] - lat) ** 2)
    km = 111.32 * math.sqrt(((best[0] - lon) * k) ** 2 + (best[1] - lat) ** 2)
    if km > PLACE_MAX_KM:
        return None
    return ("%s (%.1f km)" if km <= PLACE_NEAR_KM else "near %s (%.1f km)") % (best[2], km)


def annotate_places(facts, places):
    """Give every listed cell row its nearest named place (row["place"]), so answers say where, not just lat/lon."""
    if not places:
        return 0
    n = 0
    for block in facts.values():
        if not isinstance(block, dict):
            continue
        for v in block.values():
            if isinstance(v, list):
                for r in v:
                    c = r.get("centroid") if isinstance(r, dict) else None
                    if isinstance(c, (list, tuple)) and len(c) >= 2 and isinstance(c[0], (int, float)):
                        name = nearest_place(places, c[0], c[1])
                        if name:
                            r["place"] = name; n += 1
    return n


def works_facts(kfind):
    city = kfind("works.city_summary")
    wards = sorted(kfind("works.ward_rollup"), key=lambda o: -(o.get("paid_net_total") or 0))
    contr = sorted(kfind("works.contractor_rollup"), key=lambda o: -(o.get("paid_net_total") or 0))
    if not (city or wards or contr):
        return None
    pick = lambda o, ks: {k: o.get(k) for k in ks if o.get(k) is not None}
    return {"city_summary": [pick(o, ("works_count", "wards_count", "contractors_count", "paid_net_total", "paid_gross_total")) for o in city[:1]],
            "wards_by_spend": [pick(o, ("ward", "works_count", "paid_net_total")) for o in wards[:TOP]],
            "wards_total": len(wards),
            "contractors_by_spend": [pick(o, ("name", "contractor", "works_count", "paid_net_total")) for o in contr[:TOP]]}


def _fmt(v):
    if isinstance(v, float):
        return ("%.3f" % v).rstrip("0").rstrip(".") if abs(v) < 100 else "{:,.0f}".format(v)
    if isinstance(v, int):
        return "{:,}".format(v)
    return str(v)


# 0.4.4 — the ENGINE writes the tables (deterministic, never truncated, exact place labels for the map link); the model
# writes only a short headline + bullets. A reasoning model spent 2,859 of 3,000 tokens thinking and its table was cut
# after the header row (staging, finish=length).
COLS = {   # field → (header, scale) — percentages from fractions; everything else as stored
    "density_per_km2": ("Pop. density (/km²)", 1), "population": ("Population", 1),
    "road_density_km_per_km2": ("Road density (km/km²)", 1), "road_length_m": ("Road length (m)", 1),
    "heat_c": ("Heat (°C)", 1), "rainfall_mm": ("Rainfall (mm)", 1), "forecast_peak_precip_mm": ("Forecast peak (mm)", 1),
    "forecast_total_precip_mm": ("Forecast total (mm)", 1), "green_frac": ("Green cover (%)", 100),
    "builtup_frac": ("Built-up (%)", 100), "flood_susceptibility": ("Flood susceptibility", 1),
    "hand_mean_m": ("Height above drainage (m)", 1), "building_coverage": ("Building coverage (%)", 100),
    "mean_height_m": ("Mean height (m)", 1), "built_volume_m3": ("Built volume (m³)", 1),
    "facility_count": ("Facilities", 1), "works_count": ("Works", 1), "paid_net_total": ("Paid (₹)", 1),
    "walkability_score": ("Walkability (/100)", 1), "intersection_density_per_km2": ("Junctions (/km²)", 1),
    "transit_gap_score": ("Transit gap (/100)", 1), "dist_bus_stop_m": ("Nearest bus stop (m)", 1),
    "dist_metro_m": ("Nearest metro (m)", 1), "chokepoint_score": ("Chokepoint (/100)", 1),
}
_SKIP = {"h3", "centroid", "lat_lon", "place", "facilities"}


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v) if v is not None else "—"
    return "{:,.0f}".format(v) if abs(v) >= 1000 or float(v).is_integer() else "{:,.1f}".format(v)


def _title(key):
    k = key
    if k.upper().startswith("PROXY_"):
        k = "Proxy — " + k[6:]
    return k.replace("_", " ").strip().capitalize() if not k.startswith("Proxy") else k.replace("_", " ")


def _table(rows, first_col, first):
    fields = [f for f in COLS if any(f in r for r in rows)] + \
             sorted({f for r in rows for f in r if f not in COLS and f not in _SKIP and f != first_col and not isinstance(r[f], (dict, list))})
    head = ["#", first] + [COLS[f][0] if f in COLS else f.replace("_", " ") for f in fields]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for i, r in enumerate(rows, 1):
        loc = r.get(first_col) if first_col else (r.get("place") or r.get("lat_lon") or "—")
        vals = [_num(r.get(f) * COLS[f][1] if (f in COLS and isinstance(r.get(f), (int, float)) and COLS[f][1] != 1) else r.get(f)) for f in fields]
        out.append("| " + " | ".join([str(i), str(loc).replace("|", "/")] + vals) + " |")
    return "\n".join(out)


def fact_tables(facts):
    """Markdown tables for every ranked list in the facts: cell lists (Location = nearest place, else lat/lon) and the
    works rollups (Ward / Contractor). At most TOP rows each, as computed — the model cannot drop or reorder them."""
    out = []
    for topic in facts.get("question_topics") or []:
        block = facts.get(topic)
        if not isinstance(block, dict):
            continue
        for k, v in block.items():
            if isinstance(v, list) and v and isinstance(v[0], dict) and ("h3" in v[0] or "centroid" in v[0]):
                out.append("**%s**\n\n%s" % (_title(k), _table(v[:TOP], None, "Location")))
    ex = facts.get("excluded_as_implausible") or {}
    if ex.get("cells"):
        out.append("**Not ranked — modelled density looks implausible**\n\n" + _table(ex["cells"][:TOP], None, "Location")
                   + "\n\n_" + ex["why"] + "._")
    pw = facts.get("public_works") or {}
    if pw.get("wards_by_spend"):
        out.append("**Wards by works spend**\n\n" + _table(pw["wards_by_spend"][:TOP], "ward", "Ward"))
    if pw.get("contractors_by_spend"):
        rows = [dict(r, _who=r.get("name") or r.get("contractor")) for r in pw["contractors_by_spend"][:TOP]]
        out.append("**Contractors by works spend**\n\n" + _table([{k2: v2 for k2, v2 in r.items() if k2 not in ("name", "contractor")} for r in rows], "_who", "Contractor"))
    return out


def evidence_line(facts):
    cc = facts.get("city_cells") or {}
    used = facts.get("layers_used_for_this_question") or []
    lp = facts.get("layers_present") or {}
    bits = [str(cc.get("source") or "no city cell layers")]
    if cc.get("cells"):
        bits.append("{:,} city cells".format(cc["cells"]))
    if used:
        bits.append("layers used: " + ", ".join("%s (%s cells)" % (s, "{:,}".format(lp.get(s) or 0)) for s in used))
    if facts.get("places_source"):
        bits.append("place names: OpenStreetMap gazetteer (nearest named place)")
    return "Evidence quality: " + "; ".join(bits) + "."


def compose_answer(model_text, facts):
    """Model prose (headline + bullets) + the engine's tables + one evidence line. A cut-off reply (no evidence line)
    loses any dangling table fragment / half line, and is marked as shortened — the tables and evidence never are."""
    txt = (model_text or "").strip()
    cut = "evidence quality" not in txt.lower()
    lines = [l for l in txt.splitlines() if not l.lstrip().startswith("|")]          # tables are the engine's job
    lines = [l for l in lines if not l.lower().lstrip("*_ ").startswith("evidence quality")]
    if cut and lines and not re.search(r"[.!?:)\]*_]\s*$", lines[-1]):
        lines = lines[:-1]                                                           # drop a half-written last line
    prose = "\n".join(lines).strip()
    parts = [prose] if prose else []
    if cut and prose:
        parts.append("_(The analyst's commentary was cut short; the figures below are complete.)_")
    parts += fact_tables(facts)
    parts.append(evidence_line(facts))
    return "\n\n".join(parts)


def facts_summary(facts, why="returned no text"):
    """The computed facts as a plain briefing — used when the model returns no text, so the user always gets the
    grounded numbers (never 'No answer'). Deterministic; states nothing the facts do not."""
    lines = ["The analyst model %s, so here are the computed facts directly:" % why]
    for topic in facts.get("question_topics") or []:
        block = facts.get(topic)
        if not isinstance(block, dict):
            continue
        for k, v in block.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                rows = []
                for r in v[:5]:
                    c = r.get("centroid")
                    where = ("cell %s (lon %s, lat %s)" % (r.get("h3"), c[0], c[1])) if c else "cell %s" % r.get("h3")
                    if r.get("place"):
                        where = "%s — %s" % (r["place"], where)
                    vals = ", ".join("%s %s" % (kk, _fmt(vv)) for kk, vv in r.items() if kk not in ("h3", "centroid", "lat_lon", "place"))
                    rows.append("%s: %s" % (where, vals))
                lines.append("- %s — " % k.replace("_", " ") + "; ".join(rows))
            elif isinstance(v, dict) and {"min", "max"} <= set(v):
                lines.append("- %s: min %s, median %s, max %s across %s cells" % (k.replace("_", " "), _fmt(v["min"]), _fmt(v["median"]), _fmt(v["max"]), _fmt(v["cells"])))
            elif not isinstance(v, (list, dict)):
                lines.append("- %s: %s" % (k.replace("_", " "), _fmt(v)))
    pw = facts.get("public_works") or {}
    if pw.get("wards_by_spend"):
        lines.append("- wards by works spend: " + "; ".join("%s ₹%s (%s works)" % (w.get("ward"), _fmt(w.get("paid_net_total") or 0), _fmt(w.get("works_count") or 0)) for w in pw["wards_by_spend"][:5]))
    if facts.get("layers_missing_for_this_question"):
        lines.append("- Missing for this question: " + ", ".join(facts["layers_missing_for_this_question"]) + " — not an all-clear.")
    if len(lines) == 1:
        lines.append("- No computed facts match this question.")
    lines.append("Evidence quality: " + ", ".join("%s %s cells" % (s, _fmt((v or {}).get("cells") or 0)) for s, v in (facts.get("evidence_quality") or {}).items())
                 + " · source " + str((facts.get("city_cells") or {}).get("source")))
    return "\n".join(lines)


def h_ask(ctx):
    body = ctx.get("body") or {}
    question = (body.get("question") or body.get("q") or "").strip()[:500]
    if not question:
        return None, "question required"
    kfind = ctx["kfind"]

    cov = {}
    for o in kfind("city.coverage"):
        src = o.get("source_schema") or o.get("layer") or o.get("noun")
        if src:
            cov[src] = o
    recs = [o for o in kfind("governance.recommendation")
            if not o.get("status") or o.get("status") in ("open", "proposed", "active")]
    effect = sorted(kfind("intelligence.effectiveness"), key=lambda o: o.get("observed_at") or "", reverse=True)

    topics = topics_of(question)
    cells, layer_counts, cell_source = city_cells(kfind)
    if any(t in topics for t in ("access", "transit", "walk")):
        layer_counts.update(join_mobility(kfind, cells))
    facts = {"question_topics": topics}
    sources = []
    if cells:
        facts["city_cells"] = {"source": cell_source, "cells": len(cells)}
        for t in topics:
            if t != "works":
                tf = topic_facts(t, cells, question)
                if tf:
                    facts[t] = tf
        sources.append(cell_source.split(" ")[0] if cell_source.startswith("city.") else "raw cell layers")
    if "works" in topics or "overview" in topics:
        wf = works_facts(kfind)
        if wf:
            facts["public_works"] = wf
            sources.append("works rollups")
    crowd, crowd_used = crowd_facts(kfind, cov)
    if crowd:
        facts["mass_gathering"] = crowd
        sources += crowd_used

    try:
        places = load_places(kfind)
    except Exception:
        places = []
    if annotate_places(facts, places):
        facts["places_source"] = "space.place gazetteer (OpenStreetMap names; nearest named place to each cell centroid)"
        sources.append("space.place")
    if any(t in topics for t in ("access", "population", "services")):
        odd = sorted((c for c in cells.values() if not plausible(c)), key=lambda c: -(c.get("density_per_km2") or 0))
        if odd:
            facts["excluded_as_implausible"] = {
                "why": "GHSL modelled density above the densest 2011 census ward (%s /km²) — likely an overestimate in a "
                       "commercial core; not ranked on density" % "{:,.0f}".format(DENSITY_MAX),
                "cells": [_row(c, ("population", "density_per_km2")) for c in odd[:TOP]]}
            annotate_places({"x": facts["excluded_as_implausible"]}, places)    # name them too (built after the main pass)
    present = sorted(layer_counts)
    wanted = sorted({FIELD_LAYER[f] for t in topics for f in TOPICS.get(t, ((), ()))[1] if f in FIELD_LAYER})
    facts["layers_present"] = {s: layer_counts[s] for s in present}
    facts["layers_missing_for_this_question"] = [s for s in wanted if s not in layer_counts]
    facts["layers_used_for_this_question"] = [s for s in wanted if s in layer_counts]
    if "access" in topics:
        facts["layers_missing_for_this_question"].append("traffic / congestion (no such layer exists for this tenant)")
    facts["evidence_quality"] = {s: {"cells": layer_counts[s], "coverage_pct": (cov.get(s) or {}).get("coverage_pct")} for s in present}
    if recs:
        facts["open_recommendations"] = [{"title": r.get("title"), "action": r.get("proposed_action"), "rationale": r.get("rationale")}
                                         for r in recs[:TOP]]
        facts["open_recommendations_total"] = len(recs)
        sources.append("governance.recommendation")
    if effect:
        facts["past_interventions"] = [{"intervention": e.get("intervention"), "target": e.get("target_schema"),
                                        "direction": e.get("direction"), "delta_pct": e.get("delta_pct")} for e in effect[:TOP]]

    sysmsg = ("You are the City Intelligence Analyst for AirawatOS, advising city officials. Answer using ONLY the FACTS "
              "JSON — live, governed records computed per H3 grid cell (each cell has an h3 id and a centroid [lon, lat]) and "
              "the public-works rollups. State concrete numbers from the facts; never invent a figure, a place name or a "
              "cause. Refer to a cell by its `place` (e.g. 'near Jayanagar (1.6 km)' — keep the distance and the word 'near') and, "
              "when a cell has no place, by its lat_lon (\"lat, lon\"); never print h3 ids — the map highlights those cells. Do not "
              "name neighbourhoods the facts do not name. Anything labelled PROXY or 'proxy' must be called a proxy in the "
              "answer (say it once). If "
              "facts.layers_missing_for_this_question is non-empty, say plainly which data is missing and what it would take "
              "— a missing layer is never an all-clear. FORMAT (Markdown, rendered in a chat panel): a one-line **bold** "
              "headline, then at most 4 short '- ' bullets (name at most the top 3 places). Do NOT write any table and do NOT "
              "list every cell — the full ranked tables are appended automatically below your text. Round to at most 1 "
              "decimal, with thousands separators. Keep it under 120 words. If the question asks what to do, suggest options "
              "grounded in the facts and any open recommendations. Do NOT show reasoning or restate the question. End with "
              "the single line 'Evidence quality: ok' (the full evidence line is appended for you). You inform; officials decide.")
    msgs = [{"role": "system", "content": sysmsg},
            {"role": "user", "content": "FACTS:\n" + json.dumps(facts, default=str)[:12000] + "\n\nQUESTION: " + question}]
    try:
        ans = (ctx["ai_complete"](msgs, ANSWER_TOKENS) or "").strip()
    except Exception as e:                                             # unavailable / timed out: still answer with the facts
        return {"answer": facts_summary(facts, "is unavailable (%s)" % str(e)[:80]), "sources": sources, "facts": facts}, None
    ans = re.sub(r"(?is)<think>.*?</think>", "", ans).strip()
    _tells = ("the user wants", "the facts", "system prompt", "need to", "we need", "let me", "i should")
    if any(t in ans[:120].lower() for t in _tells):
        blocks = [b for b in re.split(r"\n\s*\n", ans) if b.strip()]
        if blocks:
            ans = blocks[-1].strip()
    if not ans.strip():                                                # the model spent its budget reasoning / returned nothing
        return {"answer": facts_summary(facts), "sources": sources, "facts": facts}, None
    return {"answer": compose_answer(ans, facts), "sources": sources, "facts": facts}, None


HANDLERS = {"city-ask": h_ask}

if __name__ == "__main__":
    print("City Intelligence — the City Scan Analyst. Verb: city-ask (grounded Q&A over governed city layers).")
