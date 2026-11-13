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
import collections, json, math, os, re, time

HOT_FLOOD = ("high", "severe")
TOP = 8
PRIORITY_TOKENS = 2600   # 0.4.8: an open 'top 5 priorities' question used all 3,000 tokens reasoning (finish=length, empty)
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
    "heat":      (("heat", "hot", "hottest", "hotter", "temperature", "cool"), ("heat_c", "population")),
    "air":       (("air", "pollut", "pm2", "pm 2", "aqi", "smog", "dust", "breath", "hotspot", "emission"), ("pm25", "population")),
    "flood":     (("flood", "water", "drain", "rain", "monsoon", "inundat", "storm"),
                  ("flood_susceptibility", "rainfall_mm", "forecast_peak_precip_mm", "population")),
    "services":  (("health", "hospital", "clinic", "school", "facility", "facilities", "service", "amenit", "centre", "center",
                   "toilet", "sanitation", "drinking water", "bank", "atm", "police", "fire station", "post office", "market",
                   "pharmac", "college", "anganwadi", "kindergarten", "library", "community cent", "playground", "garbage",
                   "recycl", "petrol", "fuel", "charging", "temple", "mosque", "church", "stadium", "sports"),
                  ("facility_count", "population")),
    # 0.5.0 — lakes (driver/lake-watch), growth (night lights 2014–2025), civic issues from news
    "lakes":     (("lake", "kere", "wetland", "water bod", "waterbod", "encroach", "reservoir"), ()),
    "growth":    (("night light", "nightlight", "economic", "economy", "growth", "grown", "grew", "develop", "brighten",
                   "prosper", "urbanis", "urbaniz", "boom", "lit up"), ()),
    "civic":     (("news", "complain", "grievance", "issue", "civic", "happening", "protest", "incident", "report"), ()),
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
    # 0.4.7 — ward-level NEED vs SPEND (census density, PM2.5, metro distance against works paid per person)
    "need":      (("per person", "per capita", "per head", "underfund", "under-fund", "neglect", "low spend", "least spend",
                   "fair share", "equit", "inequit", "deserv", "mismatch", "need vs", "need versus", "need and spend"),
                  ("census_density_per_km2", "paid_per_person", "dist_metro_m", "pm25")),
}
WHOLE_WORD = {"hot", "air", "cool", "stop", "dust"}      # 'air' ≠ airport / repair; 'hot' ≠ hotspot
NEED_WITH = ("population", "transit", "air")
PRIORITY_RE = re.compile(r"\bpriorit|\btop (3|5|three|five|ten)\b|\bif you were\b|\bresponsible for the city\b|\bbiggest (problem|issue|challenge)"
                         r"|\bmost urgent\b|\bagenda\b|\bwhere should (we|i|the city) (start|begin|focus)\b|\bwhat should (we|i|the city) (do|focus|fix)")              # works + any of these → the need-vs-spend ranking
INVENTORY_RE = re.compile(r"\b(what|which|list|show|tell me)\b.{0,40}\b(data|datasets?|layers?|sources?)\b"
                          r"|\b(data|datasets?|layers?)\b.{0,25}\b(you have|do you have|available|have access)\b"
                          r"|\bwhat can you (answer|do|tell)\b")
FIELD_LAYER.update({"pm25": "air.quality", "census_density_per_km2": "demographics.census_ward", "paid_per_person": "works.ward_cell"})
CATEGORY_WORDS = {"healthcare": ("health", "hospital", "clinic"), "education": ("school", "education", "college"),
                  "emergency": ("fire", "police", "emergency")}   # 0.4.8: keys = the facility CATEGORIES as written ('healthcare', not 'health')
# 0.5.0 — every facility category (osm-facilities 0.2.0): display name, question words, and the straight-line distance
# beyond which a populated cell counts as FAR from one (a walk for daily needs, further for less frequent ones)
FAC = {
    "healthcare": ("Health", ("health", "hospital", "clinic", "pharmac", "doctor", "medical"), 1.5),
    "education":  ("Education", ("school", "education", "college", "anganwadi", "kindergarten", "library"), 1.0),
    "emergency":  ("Safety & emergency", ("police", "fire station", "fire", "emergency", "ambulance", "safety"), 2.0),
    "government": ("Civic offices", ("government office", "civic office", "post office", "ward office", "court"), 2.0),
    "sanitation": ("Water, sanitation & waste", ("toilet", "sanitation", "drinking water", "garbage", "waste", "recycl"), 0.5),
    "transport":  ("Transport hubs", ("bus station", "railway station", "fuel", "petrol", "charging"), 2.0),
    "banking":    ("Banking access", ("bank", "atm", "cash"), 1.0),
    "welfare":    ("Social welfare", ("community cent", "welfare", "social facilit", "old age"), 2.0),
    "commerce":   ("Markets & shopping", ("market", "mall", "supermarket", "shopping"), 1.5),
    "religious":  ("Religious & cultural", ("temple", "mosque", "church", "worship", "museum", "theatre"), 1.5),
    "recreation": ("Parks & recreation", ("park", "playground", "sports", "stadium", "garden", "recreation"), 0.8),
    "utility":    ("Utilities", ("substation", "power", "water tower", "telecom"), 3.0),
}
SERVICE_CATS = ("healthcare", "education", "emergency", "government", "sanitation", "transport", "banking", "welfare")
CITY_ALIASES = {"bengaluru": ("bangalore", "bbmp", "bmtc", "bescom", "bwssb"), "bangalore": ("bengaluru",), "kolkata": ("calcutta",),
                "mumbai": ("bombay",), "chennai": ("madras",), "nashik": ("nasik",)}
PLACE_RE = re.compile(r"\b(tell me about|what about|how is|how's|how are things in|profile|describe|summari[sz]e|overview of|about)\b")


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


def _says(q, w):
    return re.search(r"\b" + re.escape(w) + (r"\b" if w in WHOLE_WORD else ""), q) is not None


def topics_of(question):
    """0.4.7: keywords match at a word start ('air' is not 'repair'; 'hot' is not 'hotspot'); a question about the data
    itself is the INVENTORY (never a topic — 'what data do you have access to' is not an access question); works plus
    people / transit / air is the ward NEED-vs-SPEND ranking, answered on its own."""
    q = question.lower()
    if INVENTORY_RE.search(q):
        return ["inventory"]
    if PRIORITY_RE.search(q):
        return ["priorities"]
    t = [k for k, (words, _) in TOPICS.items() if any(_says(q, w) for w in words)]
    if "population" in t and len(t) > 1 and any(x in t for x in ("services", "civic", "lakes", "growth")):
        t.remove("population")                           # 0.5.0: 'where are PEOPLE far from a toilet' is not a population question
    if "need" in t or ("works" in t and any(x in t for x in NEED_WITH)):
        return ["need"]
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
    elif topic == "services" and FAC_POINTS.get("_loaded"):
        return services_facts(cells, question)
    elif topic == "services":
        q = question.lower()
        cats = [c for c, words in CATEGORY_WORDS.items() if any(w in q for w in words)] or None
        alias = {"healthcare": ("healthcare", "health")}
        has = (lambda c: any((c.get("facilities") or {}).get(k2) for k in cats for k2 in alias.get(k, (k,)))) if cats \
            else (lambda c: (c.get("facility_count") or 0) > 0)
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


# ---------------------------------------------------------------- 0.4.7: air quality, ward need-vs-spend, inventory
def _try(kfind, schema, where=None):
    try:
        return kfind(schema, where) if where else kfind(schema)
    except Exception:                          # a refused / absent read is reported missing, never fatal
        return []


def join_air(kfind, cells):
    """PM2.5 of the LATEST interpolated hour onto the cells (by h3) → meta, or None. The hour comes from the newest
    hotspot run (it is computed from that hour), else the newest station hour; an hour with only a handful of cells
    (a partial run) is skipped. Never mixes hours — an older, superseded hour can never leak into the answer."""
    hours = sorted({o.get("observed_at") for o in _try(kfind, "air.hotspot") if o.get("observed_at")}, reverse=True)
    stations = _try(kfind, "air.sensor_reading")
    hours += [h for h in sorted({o.get("observed_at") for o in stations if o.get("observed_at")}, reverse=True) if h not in hours]
    for hour in hours[:4]:
        rows = [o for o in _try(kfind, "air.quality", {"observed_at": hour}) if o.get("observed_at") == hour]
        if len(rows) < 50:
            continue
        n = 0
        for o in rows:
            h, v = _h3(o), o.get("pm25")
            if not h or not isinstance(v, (int, float)):
                continue
            c = cells.get(h)
            if c is None:
                c = cells[h] = {"cell_ref": {"h3_id": h, "centroid": (o.get("space") or {}).get("centroid")}}
            c["pm25"] = v; n += 1
        latest = {}                                  # each station's NEWEST measured reading (may be newer than the surface)
        for o in stations:
            if isinstance(o.get("pm25"), (int, float)) and o.get("observed_at"):
                k = o.get("sensor_id")
                if k not in latest or o["observed_at"] > latest[k]["observed_at"]:
                    latest[k] = o
        st = list(latest.values())
        hs = [o for o in _try(kfind, "air.hotspot") if o.get("observed_at") == hour]
        return {"observed_at": hour, "cells": n, "method": (rows[0].get("method") or "idw"), "stations": st, "hotspots": hs}
    return None


def _station_name(sid):
    s = str(sid or "")
    s = re.sub(r"^cpcb-", "", s).split("--")[0]
    return " ".join(w[:1].upper() + w[1:] for w in s.replace("-", " ").split()) or "station"


def air_facts(cells, meta):
    num = lambda c: c.get("pm25") if isinstance(c.get("pm25"), (int, float)) else None
    f = {"pm25_hour": meta["observed_at"],
         "how_estimated": "%s interpolation from %d monitoring stations — an ESTIMATE between stations, not a measurement "
                          "in every cell" % (str(meta["method"]).upper(), len(meta["stations"])),
         "pm25": _stats(cells, "pm25")}
    f["most_polluted_cells"], _ = _top(cells, num, ("pm25", "population"))
    rows, _ = _top(cells, lambda c: (c.get("pm25") or 0) * (c.get("population") or 0), ("pm25", "population"),
                   cond=lambda c: num(c) is not None and c.get("population") is not None and plausible(c))
    f["exposure_cells (PM2.5 × people)"] = rows
    f["hotspots (top 5% of the city surface)"] = [
        {"h3": _h3(o), "centroid": (o.get("space") or {}).get("centroid"), "pm25": o.get("value"),
         "severity": o.get("severity"), "hotspot_cells": o.get("cell_count")}
        for o in sorted(meta["hotspots"], key=lambda o: -(o.get("value") or 0))[:TOP]]
    f["stations (measured)"] = [
        {"h3": _h3(o), "centroid": (o.get("space") or {}).get("centroid"), "station": _station_name(o.get("sensor_id")),
         "pm25": o.get("pm25"), "reading_at": (o.get("observed_at") or "")[11:16] + " UTC"} for o in sorted(meta["stations"], key=lambda o: -(o.get("pm25") or 0))[:TOP]]
    return {k: v for k, v in f.items() if v not in (None, [], {})}


def _pct_rank(vals):
    """value → percentile rank in [0, 1] (ties share the lower rank), as Postgres percent_rank."""
    v = sorted(vals); n = len(v)
    import bisect
    return (lambda x: (bisect.bisect_left(v, x) / (n - 1)) if n > 1 else 0.0)


def need_facts(kfind, cells, places=None):
    """0.4.7 — the WARD need-vs-spend ranking. Per ward (2011 census wards = the works data's ward numbering): census
    density, the mean PM2.5 / straight-line metro distance / share of cells far from transit over the ward's cells, and
    works paid per person (all works paid 2012–2022 ÷ census population). need_vs_spend = the mean of four percentile
    ranks (denser, dirtier air, further from metro, LESS paid per person → higher) — an equal-weight ranking aid, not a
    verdict. → (facts, map_cells, layer_counts)."""
    cw = _try(kfind, "demographics.census_ward")
    if not cw:
        return None, [], {}
    wards = {}
    for o in cw:
        w = str(o.get("ward_no") or "")
        h = _h3(o)
        if not w or not h:
            continue
        d = wards.setdefault(w, {"name": o.get("ward_name"), "pop": o.get("census_population"),
                                 "dens": o.get("census_density_per_km2"), "cells": []})
        d["cells"].append(h)
    counts = {"demographics.census_ward": len(cw)}
    wk = _try(kfind, "works.ward_cell")
    years = set()
    for o in wk:
        d = wards.get(str(o.get("ward_no") or ""))
        if d is not None and "paid" not in d:
            d["paid"] = o.get("paid_net_total"); d["works"] = o.get("works_count")
            years |= {str(s.get("t")) for s in (o.get("series") or []) if s.get("t")}
    if wk:
        counts["works.ward_cell"] = len(wk)
    for o in _try(kfind, "procurement.ward_cell"):
        d = wards.get(str(o.get("ward_no") or ""))
        if d is not None and "tenders" not in d:
            d["tenders"] = o.get("tenders_count")
    mean = lambda xs: (sum(xs) / len(xs)) if xs else None
    for d in wards.values():
        cs = [cells.get(h) or {} for h in d["cells"]]
        d["pm25"] = mean([c["pm25"] for c in cs if isinstance(c.get("pm25"), (int, float))])
        d["metro"] = mean([c["dist_metro_m"] for c in cs if isinstance(c.get("dist_metro_m"), (int, float))])
        gaps = [c["transit_gap_score"] for c in cs if isinstance(c.get("transit_gap_score"), (int, float))]
        d["under"] = (sum(1 for g in gaps if g > 0) / len(gaps)) if gaps else None
        d["ppp"] = (d["paid"] / d["pop"]) if isinstance(d.get("paid"), (int, float)) and d.get("pop") else None
    dims = (("dens", False), ("pm25", False), ("metro", False), ("ppp", True))     # (field, lower-is-needier)
    ranks = {}
    for f, low in dims:
        vals = [d[f] for d in wards.values() if isinstance(d.get(f), (int, float))]
        if len(vals) > 1:
            pr = _pct_rank(vals)
            ranks[f] = (lambda x, pr=pr, low=low: (1 - pr(x)) if low else pr(x))
    for d in wards.values():
        rs = [ranks[f](d[f]) for f, _ in dims if f in ranks and isinstance(d.get(f), (int, float))]
        d["score"] = round(100 * sum(rs) / len(rs)) if len(rs) >= 2 else None

    def rep_cell(d):                       # the ward cell nearest its cells' mean centroid — one anchor for the map
        pts = [(h, (cells.get(h) or {}).get("cell_ref", {}).get("centroid")) for h in d["cells"]]
        pts = [(h, c) for h, c in pts if isinstance(c, (list, tuple)) and len(c) >= 2]
        if not pts:
            return d["cells"][0], None
        mx, my = mean([c[0] for _, c in pts]), mean([c[1] for _, c in pts])
        return min(pts, key=lambda p: (p[1][0] - mx) ** 2 + (p[1][1] - my) ** 2)

    ranked = sorted((d for d in wards.values() if d["score"] is not None), key=lambda d: -d["score"])
    label = lambda w, d: "%s (ward %s)" % (d.get("name") or "Ward", w)
    wid = {id(d): w for w, d in wards.items()}
    rows, map_cells = [], []
    for d in ranked[:TOP]:
        w = wid[id(d)]
        r = {"h3": rep_cell(d)[0], "place": label(w, d), "census_density_per_km2": d.get("dens"),
             "census_population": d.get("pop"), "pm25": d.get("pm25"), "dist_metro_m": d.get("metro"),
             "underserved_share": d.get("under"), "works_count": d.get("works"), "paid_per_person": d.get("ppp"),
             "need_vs_spend": d["score"]}
        rows.append({k: (round(v, 1) if isinstance(v, float) else v) for k, v in r.items() if v is not None})
        map_cells += [{"h3": h, "place": r["place"]} for h in d["cells"]]

    def med(f):
        v = sorted(d[f] for d in wards.values() if isinstance(d.get(f), (int, float)))
        return round(v[len(v) // 2], 1) if v else None
    yrs = sorted(years)
    f = {"wards": len(wards),
         "how_ranked": "need_vs_spend = mean of 4 percentile ranks across all wards: denser (census 2011), worse PM2.5 "
                       "(latest hour, interpolated), further from metro (straight line), LESS works paid per person — "
                       "equal weights, a RANKING AID, not a verdict",
         "works_period": ("%s–%s" % (yrs[0], yrs[-1])) if yrs else "all recorded years",
         "city_median_ward": {"census_density_per_km2": med("dens"), "pm25": med("pm25"), "dist_metro_m": med("metro"),
                              "paid_per_person": med("ppp")},
         "caveats": ["population and ward boundaries are the 2011 census (198 wards; the works data uses the same numbering)",
                     "works paid per person divides all works paid over the period by the 2011 census population",
                     "PM2.5 is interpolated between a few stations — small differences between wards are within its error"],
         "wards_with_most_need_for_least_spend": rows}
    return f, map_cells, counts


CPCB_PM25_24H = 60.0          # India's national ambient standard for PM2.5, 24-hour mean (µg/m³)
FLOOD_HIGH = 0.9              # flood_susceptibility at or above → the lowest-lying cells
HEALTH_FAR_KM = 1.5
BUS_FAR_M, METRO_FAR_M = 800, 2000


def _nearest_km(pts, lon, lat, cell=0.01):
    """Distance (km) to the nearest point in a pre-gridded {(ix, iy): [(lon, lat)]} — ring search, city scale."""
    k = math.cos(math.radians(lat)); ix, iy = int(lon / cell), int(lat / cell); best = float("inf")
    for r in range(0, 12):
        for i in range(-r, r + 1):
            for j in range(-r, r + 1):
                if max(abs(i), abs(j)) != r:
                    continue
                for (a, b) in pts.get((ix + i, iy + j), ()):
                    best = min(best, 111.32 * math.hypot((a - lon) * k, b - lat))
        if best <= r * cell * 111.32 * k:
            break
    return best


def _where(cells_sorted, places, n=3):
    out = []
    for c in cells_sorted:
        cen = (c.get("cell_ref") or {}).get("centroid") or []
        name = nearest_place(places, cen[0], cen[1]) if len(cen) >= 2 else None
        name = re.sub(r"^near ", "", re.sub(r" \([0-9.]+ km\)$", "", name)) if name else None
        if name and name not in out:
            out.append(name)
        if len(out) >= n:
            break
    return ", ".join(out) or "—"


def priorities_facts(kfind, cells, places, air_meta, need):
    """0.4.8 — CANDIDATE PRIORITIES computed by the engine (the model only chooses and phrases): each with the people
    affected (GHSL population; implausible cells excluded), where (top named places), the measure and a caveat. Ranked by
    people affected — a starting order, not a weighting. Layers that cannot support a priority are said, not ranked."""
    live = [c for c in cells.values() if plausible(c) and isinstance(c.get("population"), (int, float))]
    ppl = lambda cs: int(round(sum(c.get("population") or 0 for c in cs)))
    cand, not_ranked = [], []
    fl = [c for c in live if (c.get("flood_susceptibility") or 0) >= FLOOD_HIGH]
    if fl:
        cand.append({"priority": "Flood-prone low-lying areas", "people_affected": ppl(fl),
                     "where": _where(sorted(fl, key=lambda c: -(c["population"] * c["flood_susceptibility"])), places),
                     "measure": "people in the lowest-lying cells (flood susceptibility ≥ %.1f, height above drainage)" % FLOOD_HIGH,
                     "caveat": "terrain-based susceptibility, not observed flooding", "evidence": "cell.hydrology"})
    # stricter than the driver's 'underserved' (> 500 m from a bus stop), which counts cells only just past the line
    # (a Shivajinagar cell 520 m from a stop topped it): a ~10-minute walk to a bus AND no metro within 2 km
    tr = [c for c in live if (c.get("dist_bus_stop_m") or 0) > BUS_FAR_M and (c.get("dist_metro_m") or 0) > METRO_FAR_M]
    if tr:
        cand.append({"priority": "Transit gaps (far from bus and metro)", "people_affected": ppl(tr),
                     "where": _where(sorted(tr, key=lambda c: -(c["dist_bus_stop_m"] * c["population"])), places),
                     "measure": "people more than %d m from a bus stop AND %.0f km from a metro station (straight line)" % (BUS_FAR_M, METRO_FAR_M / 1000),
                     "caveat": "OSM stops; straight-line distance, not walking routes or frequency", "evidence": "mobility.transit_access"})
    hp = FAC_POINTS.get("healthcare") or collections.defaultdict(list)   # 0.5.0: the live facility points when loaded
    if not FAC_POINTS.get("healthcare"):
        for c in cells.values():
            f = c.get("facilities") or {}
            cen = (c.get("cell_ref") or {}).get("centroid") or []
            if (f.get("healthcare") or f.get("health")) and len(cen) >= 2:
                hp[(int(cen[0] / 0.01), int(cen[1] / 0.01))].append((cen[0], cen[1]))
    if hp:
        far = [c for c in live if c["population"] > 0 and len((c.get("cell_ref") or {}).get("centroid") or []) >= 2
               and _nearest_km(hp, *c["cell_ref"]["centroid"][:2]) > HEALTH_FAR_KM]
        if far:
            cand.append({"priority": "Health access (no facility nearby)", "people_affected": ppl(far),
                         "where": _where(sorted(far, key=lambda c: -c["population"]), places),
                         "measure": "people more than %.1f km (straight line) from any mapped health facility" % HEALTH_FAR_KM,
                         "caveat": "OSM-mapped facilities; size and capacity unknown", "evidence": "geo.facility"})
    rows = (need or {}).get("wards_with_most_need_for_least_spend") or []
    if rows:
        cand.append({"priority": "Under-funded dense wards (need vs spend)", "people_affected": int(sum(r.get("census_population") or 0 for r in rows)),
                     "where": ", ".join(r["place"] for r in rows[:3]),
                     "measure": "the %d wards with the most need for the least works spend per person (%s)" % (len(rows), need.get("works_period")),
                     "caveat": "2011 census population; equal-weight ranking aid", "evidence": "works.ward_cell + demographics.census_ward"})
    if air_meta:
        hi = [c for c in live if (c.get("pm25") or 0) > CPCB_PM25_24H]
        hot = sorted((c for c in live if isinstance(c.get("pm25"), (int, float))), key=lambda c: -c["pm25"])
        cand.append({"priority": "Air pollution exposure", "people_affected": ppl(hi),
                     "where": _where(hot, places),
                     "measure": "people where the latest hour's PM2.5 is above India's 24-hour standard (%d µg/m³)" % CPCB_PM25_24H,
                     "caveat": "one hour, interpolated from %d stations — confirm with a 24-hour mean" % len(air_meta["stations"]),
                     "evidence": "air.quality"})
    cand.sort(key=lambda r: -r["people_affected"])
    hs = _stats(cells, "heat_c")
    if hs and hs["max"] - hs["min"] < 3:
        not_ranked.append("heat — the layer varies only %.1f °C across the city, so it does not single out places" % (hs["max"] - hs["min"]))
    gs = _stats(cells, "green_frac")
    if gs and gs["median"] < 0.02:
        not_ranked.append("green cover — the layer looks unreliable (the median cell is %.1f%% green), so it is not used" % (100 * gs["median"]))
    issues = _try(kfind, "civic.issue")
    sig = collections.Counter(o.get("category") for o in issues if o.get("category") not in (None, "other"))
    out = {"how_ranked": "by people affected (GHSL population; cells denser than the densest census ward excluded) — a starting order, not a weighting",
           "candidates": cand, "not_ranked": not_ranked}
    if issues:
        out["news_signal"] = {"civic_issues": len(issues), "high_severity": sum(1 for o in issues if o.get("severity") == "high"),
                              "top_categories": dict(sig.most_common(4))}
    return out


# ---------------------------------------------------------------------------------------------------------- 0.5.0
FAC_POINTS = {}        # category → gridded points {(ix, iy): [(lon, lat)]}, plus counts; filled per ask by load_facilities


def _centroid_of(o):
    for k in ("centroid",):
        c = o.get(k)
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            return [float(c[0]), float(c[1])]
    for k in ("space", "cell_ref", "location", "geo"):
        c = (o.get(k) or {}).get("centroid") if isinstance(o.get(k), dict) else None
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            return [float(c[0]), float(c[1])]
    g = o.get("geometry") or {}
    if g.get("type") == "Point" and len(g.get("coordinates") or []) >= 2:
        return [float(g["coordinates"][0]), float(g["coordinates"][1])]
    return None


def load_facilities(kfind):
    """geo.facility points by category (osm-facilities), gridded for nearest-distance lookups. → {category: n}."""
    FAC_POINTS.clear()
    counts = {}
    for o in _try(kfind, "geo.facility"):
        cat = o.get("category"); c = _centroid_of(o)
        if not cat or not c or cat == "ai-city-site":
            continue
        FAC_POINTS.setdefault(cat, collections.defaultdict(list))[(int(c[0] / 0.01), int(c[1] / 0.01))].append((c[0], c[1]))
        counts[cat] = counts.get(cat, 0) + 1
    FAC_POINTS["_loaded"] = bool(counts); FAC_POINTS["_counts"] = counts
    return counts


def services_facts(cells, question):
    """0.5.0: for each facility category asked about, the PEOPLE farther than its threshold from the nearest one
    (straight line) and the most populous such cells — not 'no facility inside the 0.1 km² cell'."""
    q = question.lower()
    cats = [k for k, (_, words, _) in FAC.items() if any(_says(q, w) for w in words)]
    counts = FAC_POINTS.get("_counts") or {}
    f = {"facility_counts": {FAC[k][0]: counts.get(k, 0) for k in FAC if counts.get(k)},
         "how_measured": "straight-line distance from each populated cell's centre to the nearest mapped facility "
                         "(OpenStreetMap — coverage varies by category; size and capacity unknown)"}
    if not cats:
        f["category_asked"] = "any (name one — e.g. toilets, schools, police, banks, parks — for a distance ranking)"
        return f
    live = [c for c in cells.values() if plausible(c) and (c.get("population") or 0) > 0
            and len((c.get("cell_ref") or {}).get("centroid") or []) >= 2]
    f["category_asked"] = [FAC[k][0] for k in cats[:3]]
    for k in cats[:3]:
        label, _, thr = FAC[k]; pts = FAC_POINTS.get(k)
        if not pts:
            f["%s: none mapped" % label] = "no %s facilities in the data — this is missing data, not an all-clear" % label.lower()
            continue
        far = []
        for c in live:
            d = _nearest_km(pts, *c["cell_ref"]["centroid"][:2])
            if d > thr:
                far.append((c, d))
        f["people_beyond_%s_km_of_%s" % (thr, label.lower().replace(" ", "_").replace(",", ""))] = int(round(sum(c["population"] for c, _ in far)))
        rows = []
        for c, d in sorted(far, key=lambda x: -x[0]["population"])[:TOP]:
            r = _row(c, ("population",)); r["nearest_km"] = round(d, 2); rows.append(r)
        f["populous_cells_far_from_%s (> %s km, straight line)" % (label.lower(), thr)] = rows
    return f


def lake_facts(kfind, question):
    """0.5.0 — lakes (driver/lake-watch, JRC 1995–2021): which shrank most, where the shore was built up most, and the
    year-by-year detail for a lake named in the question. → (facts, named lake ids)."""
    lakes = [o for o in _try(kfind, "water.lake") if o.get("name")]
    if not lakes:
        return None, []
    q = question.lower()
    core = lambda n: re.sub(r"\b(lake|kere|tank|wetland)\b", "", n.lower()).strip()
    named = [o for o in lakes if len(core(o["name"])) >= 4 and core(o["name"]) in q]
    ha = lambda v: round(v / 1e4, 1) if isinstance(v, (int, float)) else None
    def row(o):
        return {"h3": (o.get("cell_ref") or {}).get("h3_id"), "place": o["name"], "baseline_ha": ha(o.get("baseline_water_m2")),
                "recent_ha": ha(o.get("recent_water_m2")), "water_change_pct": o.get("water_change_pct"),
                "shore_built_1975": o.get("shore_built_1975"), "shore_built_2020": o.get("shore_built_2020")}
    chg = [o for o in lakes if isinstance(o.get("water_change_pct"), (int, float)) and (o.get("baseline_water_m2") or 0) >= 50000]
    ch = sorted(o["water_change_pct"] for o in chg)
    f = {"lakes_measured": len(lakes), "lakes_with_a_change_figure (baseline ≥ 5 ha)": len(chg),
         "median_change_pct": ch[len(ch) // 2] if ch else None,
         "shrank_more_than_25pct": sum(1 for v in ch if v < -25), "grew_more_than_25pct": sum(1 for v in ch if v > 25),
         "how_measured": "water area from JRC Global Surface Water (Landsat, 30 m): mean of 1995–99 vs 2017–21; shore = a 200 m ring, GHSL built-up 1975 vs 2020",
         "caveats": ["weeds, hyacinth or algae on the water read as land, and desilting or a dry year as loss",
                     "OSM outlines; a lake's official boundary may differ"],
         "lakes_that_shrank_most": [row(o) for o in sorted(chg, key=lambda o: o["water_change_pct"])[:TOP]],
         "most_built_up_shores (2020)": [row(o) for o in sorted((o for o in chg if isinstance(o.get("shore_built_2020"), (int, float))),
                                                               key=lambda o: -o["shore_built_2020"])[:5]]}
    if named:
        o = max(named, key=lambda o: len(o["name"]))
        f["lake_asked"] = dict(row(o), osm_area_ha=ha(o.get("osm_area_m2")),
                               by_year=[{"year": s.get("t"), "water_ha": ha(s.get("water_area_m2")), "source": s.get("source")}
                                        for s in (o.get("series") or []) if int(s.get("t") or 0) % 5 == 0 or int(s.get("t") or 0) >= 2017])
    return f, [o["id"] for o in (named or [])] or [o["id"] for o in sorted(chg, key=lambda o: o["water_change_pct"])[:TOP]]


def growth_facts(kfind):
    """0.5.0 — night lights 2014–2025 (VIIRS yearly median): the city's brightening by year, where it grew most and
    where it is newly lit. A satellite PROXY for economic / energy activity, not household income."""
    rows = _try(kfind, "econ.nightlight_trend")
    if not rows:
        return None
    years = {}
    for o in rows:
        for sp in (o.get("series") or []):
            if isinstance(sp.get("avg_radiance"), (int, float)):
                years.setdefault(sp["t"], []).append(sp["avg_radiance"])
    by_year = [{"year": t, "city_mean_radiance": round(sum(v) / len(v), 2)} for t, v in sorted(years.items())]
    def cellrow(o):
        r = {"h3": (o.get("cell_ref") or {}).get("h3_id"), "centroid": (o.get("cell_ref") or {}).get("centroid"),
             "change_pct": o.get("change_pct"), "avg_radiance": o.get("avg_radiance")}
        s0 = [sp.get("avg_radiance") for sp in (o.get("series") or [])[:3] if isinstance(sp.get("avg_radiance"), (int, float))]
        r["radiance_first_years"] = round(sum(s0) / len(s0), 2) if s0 else None
        return r
    bright = [o for o in rows if isinstance(o.get("change_pct"), (int, float)) and (cellrow(o)["radiance_first_years"] or 0) >= 5]
    ch = sorted(o["change_pct"] for o in rows if isinstance(o.get("change_pct"), (int, float)))
    newly = [o for o in rows if (cellrow(o)["radiance_first_years"] or 99) < 3 and (o.get("avg_radiance") or 0) >= 10]
    first, last = (by_year[0], by_year[-1]) if by_year else ({}, {})
    return {"years": "%s–%s" % (first.get("year"), last.get("year")), "cells": len(rows), "city_mean_by_year": by_year,
            "city_change_pct": round(100 * (last["city_mean_radiance"] - first["city_mean_radiance"]) / first["city_mean_radiance"], 1)
                               if first.get("city_mean_radiance") else None,
            "median_cell_change_pct": ch[len(ch) // 2] if ch else None, "newly_lit_cells": len(newly),
            "how_measured": "VIIRS DNB monthly composites, yearly median, per 250 m cell; change = mean of the last 3 years vs the first 3",
            "PROXY": "night light tracks economic / energy activity (commercial areas, roads and industry glow most) — not household income",
            "brightened_most (already lit in 2014–16)": [cellrow(o) for o in sorted(bright, key=lambda o: -o["change_pct"])[:TOP]],
            "newly_lit (dark in the first years, lit now)": [cellrow(o) for o in sorted(newly, key=lambda o: -(o.get("avg_radiance") or 0))[:TOP]]}


def civic_facts(kfind, places):
    """0.5.0 — civic issues classified from local news: what they are about, how severe, the latest serious ones. Only
    LOCAL issues count: placed on the map, or naming the city (its AOI name / alias) or one of its localities — the news
    feeds also carry national stories (a derailment in Assam is not a Bengaluru civic issue)."""
    allissues = _try(kfind, "civic.issue")
    if not allissues:
        return None
    city = set()
    for a in _try(kfind, "space.aoi"):
        for w in re.findall(r"[a-z]{4,}", str(a.get("name") or a.get("label") or "").lower()):
            city.add(w)
    city |= {al for w in list(city) for al in CITY_ALIASES.get(w, ())}
    names = {n.lower() for (_, _, n) in (places or []) if len(n) >= 6}
    def local(o):
        if _centroid_of(o):
            return True
        t = str(o.get("title") or "").lower()
        return any(w in t for w in city) or any(n in t for n in names)
    issues = [o for o in allissues if local(o)]
    if not issues:
        return {"issues": 0, "not_local": len(allissues), "note": "no issue in the news feed is placed in or names this city"}
    cats = collections.Counter(o.get("category") or "other" for o in issues)
    sev = collections.Counter(o.get("severity") or "unknown" for o in issues)
    rank = {"high": 0, "medium": 1, "low": 2}
    serious = sorted((o for o in issues if o.get("severity") in ("high", "medium") and (o.get("category") or "other") != "other"),
                     key=lambda o: (rank.get(o.get("severity"), 3), "" if not o.get("observed_at") else "~" + o["observed_at"][::-1]))
    serious = sorted(serious, key=lambda o: (rank.get(o.get("severity"), 3), [-ord(ch) for ch in (o.get("observed_at") or "")]))
    def row(o):
        r = {"title": re.sub(r"\s+-\s+[^-]{2,40}$", "", str(o.get("title") or ""))[:110], "category": (o.get("category") or "").replace("_", " "),
             "severity": o.get("severity"), "date": (o.get("observed_at") or o.get("published_at") or "")[:10]}
        c = _centroid_of(o)
        if c:
            nm = nearest_place(places, c[0], c[1]) if places else None
            if nm: r["where"] = nm
        return r
    located = sum(1 for o in issues if _centroid_of(o))
    return {"issues": len(issues), "not_local (national / other cities, left out)": len(allissues) - len(issues), "placed_on_the_map": located,
            "by_category (excluding 'other')": dict((k.replace("_", " "), v) for k, v in cats.most_common() if k != "other"),
            "other_or_not_civic": cats.get("other", 0), "by_severity": dict(sev),
            "how_classified": "local news (RSS / GDELT) classified by a model into civic categories; many articles are not civic ('other')",
            "latest_serious": [row(o) for o in serious[:TOP]]}


def works_trend(kfind):
    """0.5.0 — works paid per YEAR across the city (works.ward_cell series, one per ward) and the wards with most tenders."""
    seen, by = set(), {}
    for o in _try(kfind, "works.ward_cell"):
        w = str(o.get("ward_no") or "")
        if not w or w in seen:
            continue
        seen.add(w)
        for sp in (o.get("series") or []):
            t = str(sp.get("t") or "")
            if t:
                d = by.setdefault(t, {"year": t, "works_count": 0, "paid_net_total": 0.0})
                d["works_count"] += sp.get("works_count") or 0; d["paid_net_total"] += sp.get("paid_net_total") or 0
    tw, tseen = [], set()
    for o in _try(kfind, "procurement.ward_cell"):
        w = str(o.get("ward_no") or "")
        if w and w not in tseen:
            tseen.add(w); tw.append({"ward": "%s (ward %s)" % (o.get("ward_name") or "Ward", w), "tenders_count": o.get("tenders_count") or 0})
    return {"works_by_year": [by[t] for t in sorted(by)],
            "wards_by_tenders": sorted(tw, key=lambda r: -r["tenders_count"])[:TOP]} if (by or tw) else None


def detect_place(kfind, question):
    """A named ward / locality / lake in the question ('tell me about Nilasandra'). Longest name wins; names under 4
    letters are ignored. → {name, kind, centroid, ward_no?, cells?} or None."""
    q = " " + re.sub(r"[^a-z0-9 ]", " ", question.lower()) + " "
    cands = []
    wards = {}
    for o in _try(kfind, "demographics.census_ward"):
        w = str(o.get("ward_no") or ""); h = _h3(o); c = (o.get("cell_ref") or {}).get("centroid")
        if w and o.get("ward_name"):
            d = wards.setdefault(w, {"name": o["ward_name"], "kind": "ward", "ward_no": w, "cells": [], "pts": []})
            if h: d["cells"].append(h)
            if c: d["pts"].append(c)
    wc = {str(o.get("ward_no")): o.get("centroid") for o in _try(kfind, "space.ward") if o.get("ward_no") and o.get("centroid")}
    for d in wards.values():
        if d["pts"]:
            d["centroid"] = [sum(p[0] for p in d["pts"]) / len(d["pts"]), sum(p[1] for p in d["pts"]) / len(d["pts"])]
        elif wc.get(d["ward_no"]):                                  # cells without centroids → the ward boundary's centre
            d["centroid"] = list(wc[d["ward_no"]])[:2]
        if d.get("centroid"):
            cands.append(d)                                         # wards first: on an equal-length match a ward wins
    for p in _try(kfind, "space.place"):
        if p.get("name") and len(p.get("centroid") or []) >= 2 and p.get("kind") in ("locality", "ward"):
            cands.append({"name": p["name"], "kind": "locality", "centroid": p["centroid"]})
    for o in _try(kfind, "water.lake"):
        c = (o.get("cell_ref") or {}).get("centroid")
        if o.get("name") and c:
            cands.append({"name": o["name"], "kind": "lake", "centroid": c, "lake": o})
    hit = None
    for d in cands:
        n = re.sub(r"[^a-z0-9 ]", " ", d["name"].lower()).strip()
        n2 = re.sub(r"\s+(ward|lake|kere|layout|nagar|nagara)$", "", n)
        for k in {n, n2}:
            if len(k) >= 4 and (" " + k + " ") in q and (hit is None or len(k) > hit[0]):
                hit = (len(k), d)
    return hit[1] if hit else None


def place_facts(place, cells, places, need=None):
    """0.5.0 — a profile of one named place: the city layers around it (its ward's cells, or the cells within 1 km)
    against the city median, facilities nearby by category, the nearest lake. → (facts, map_cells)."""
    lon, lat = place["centroid"][:2]; k = math.cos(math.radians(lat))
    near = lambda c, km: (lambda cc: bool(cc) and 111.32 * math.hypot((cc[0] - lon) * k, cc[1] - lat) <= km)((c.get("cell_ref") or {}).get("centroid"))
    pick = [cells[h] for h in place.get("cells") or [] if h in cells] or [c for c in cells.values() if near(c, 1.0)]
    if not pick:
        return None, []
    def mean(f, cs=pick, cond=lambda c: True):
        v = [c[f] for c in cs if isinstance(c.get(f), (int, float)) and cond(c)]
        return (sum(v) / len(v)) if v else None
    def cmed(f):                                   # the median over POPULATED cells (≥ 100 people) — not empty farmland
        v = sorted(c[f] for c in cells.values() if isinstance(c.get(f), (int, float)) and plausible(c) and (c.get("population") or 0) >= 100)
        return v[len(v) // 2] if v else None
    metrics = []
    for f, label, fmt in (("density_per_km2", "Population density (/km², 2020 model)", 0), ("pm25", "PM2.5 now (µg/m³, estimated)", 1),
                          ("flood_susceptibility", "Flood susceptibility (0–1)", 2), ("heat_c", "Heat (°C)", 1),
                          ("dist_bus_stop_m", "Nearest bus stop (m)", 0), ("dist_metro_m", "Nearest metro (m)", 0),
                          ("walkability_score", "Walkability (/100)", 0), ("road_density_km_per_km2", "Road density (km/km²)", 1),
                          ("building_coverage", "Building coverage (share)", 2)):
        here = mean(f, cond=plausible) if f == "density_per_km2" else mean(f)
        if here is not None:
            med = cmed(f)
            metrics.append({"metric": label, "here": round(here, fmt) if fmt else round(here), "city_median": (round(med, fmt) if fmt else round(med)) if med is not None else None})
    fac = {}
    for cat, (label, _, _) in FAC.items():
        pts = FAC_POINTS.get(cat)
        if not pts:
            continue
        n = sum(1 for cell in pts.values() for (x, y) in cell if 111.32 * math.hypot((x - lon) * k, y - lat) <= 1.0)
        if n:
            fac[label] = n
    nearest = {}
    for cat in ("healthcare", "education", "emergency", "sanitation", "banking", "recreation"):
        pts = FAC_POINTS.get(cat)
        if pts:
            d = _nearest_km(pts, lon, lat)
            if d < 50:
                nearest[FAC[cat][0]] = "%.1f km" % d
    f = {"name": place["name"], "kind": place["kind"],
         "area": ("its %d ward cells" % len(pick)) if place.get("cells") else "the %d cells within 1 km" % len(pick),
         "profile": metrics, "facilities_within_1_km": fac, "nearest_facility (straight line)": nearest}
    if place.get("lake"):
        o = place["lake"]
        f["lake"] = {"baseline_ha": round((o.get("baseline_water_m2") or 0) / 1e4, 1), "recent_ha": round((o.get("recent_water_m2") or 0) / 1e4, 1),
                     "water_change_pct": o.get("water_change_pct"), "shore_built_1975": o.get("shore_built_1975"), "shore_built_2020": o.get("shore_built_2020")}
    if need and place.get("ward_no"):
        f["ward_vs_city"] = {"city_median_ward": need.get("city_median_ward"),
                             "this_ward_in_the_need_vs_spend_top": any(r.get("place", "").endswith("(ward %s)" % place["ward_no"])
                                                                       for r in need.get("wards_with_most_need_for_least_spend") or [])}
    label = place["name"] + (" (ward %s)" % place["ward_no"] if place.get("ward_no") else "")
    f["label"] = label
    return f, [{"h3": _h3(c), "place": label} for c in pick if _h3(c)]


INVENTORY = [   # (schema, friendly name, unit, what it is) — the order the inventory table lists them
    ("demographics.population", "Population (GHSL 2020 model)", "cells", "people per 250 m grid cell, modelled"),
    ("demographics.census_ward", "Census 2011 by ward", "cells", "ward population and density"),
    ("air.quality", "Air quality — PM2.5 surface", "cells", "latest hour, interpolated from stations"),
    ("air.sensor_reading", "Air monitoring stations (CPCB)", "readings", "measured PM2.5 / NO₂ / O₃"),
    ("air.hotspot", "Air pollution hotspots", "hotspots", "top 5% of the PM2.5 surface"),
    ("cell.heat", "Heat", "cells", "land surface temperature"),
    ("cell.hydrology", "Flood susceptibility", "cells", "height above nearest drainage"),
    ("cell.rainfall", "Rainfall (normal)", "cells", "annual climate normal"),
    ("cell.weather_forecast", "Rain forecast (48 h)", "cells", "forecast precipitation"),
    ("cell.greencover", "Green cover", "cells", "vegetation share"),
    ("cell.builtup", "Built-up area", "cells", "built-up share"),
    ("cell.buildings", "Buildings", "cells", "coverage, height, volume"),
    ("cell.roads", "Road density", "cells", "OSM road length per km²"),
    ("geo.facility", "Facility points (all categories)", "points", "health, education, safety, civic offices, sanitation, banking, parks…"),
    ("water.lake", "Lakes (Lake Watch)", "lakes", "water extent 1995–2021, shore built-up 1975–2020"),
    ("econ.nightlight_trend", "Night lights 2014–2025", "cells", "yearly satellite radiance — a proxy for activity"),
    ("geo.building", "Building outlines", "buildings", "two areas so far (south strip, MG Road–Jayanagar)"),
    ("cell.aod", "Satellite haze (AOD)", "cells", "MODIS MAIAC, partial (cloud)"),
    ("cell.cams", "Regional PM2.5 background", "cells", "Copernicus CAMS"),
    ("space.ward", "Wards", "wards", "BBMP 198-ward boundaries"),
    ("mobility.transit_access", "Transit access", "cells", "distance to bus stop / metro"),
    ("mobility.walkability", "Walkability", "cells", "street-grid score"),
    ("mobility.chokepoint", "Road-network chokepoints", "cells", "route concentration — a proxy, not traffic"),
    ("works.ward_cell", "Public works by ward", "cells", "works and amounts paid, 2012–2022"),
    ("procurement.ward_cell", "Tenders by ward", "cells", "tender counts"),
    ("news.item", "News articles", "articles", "local news (RSS / GDELT)"),
    ("civic.issue", "Civic issues from news", "issues", "classified, geocoded where possible"),
    ("space.place", "Place names (gazetteer)", "places", "OpenStreetMap localities and wards"),
    ("governance.recommendation", "Open recommendations", "items", "for officials to review"),
]


NO_AS_OF = {"works.ward_cell", "procurement.ward_cell", "space.place", "water.lake", "geo.facility", "space.ward"}   # their observed_at is the build time, not the data's


def inventory_facts(kfind, layer_counts, air_meta):
    rows = []
    for schema, name, unit, what in INVENTORY:
        if schema == "air.quality" and air_meta:
            n, as_of = air_meta["cells"], air_meta["observed_at"][:16].replace("T", " ") + " UTC"
        elif schema in layer_counts and schema != "geo.facility":     # (layer_counts has cells-with-a-facility, not points)
            n, as_of = layer_counts[schema], None
        else:
            rs = _try(kfind, schema)
            n = len(rs)
            ts = [o.get("observed_at") for o in rs if isinstance(o.get("observed_at"), str)]
            as_of = (max(ts)[:10] if ts and schema not in NO_AS_OF else None)
        if n:
            rows.append({"layer": name, "records": n, "unit": unit, "what": what, **({"as_of": as_of} if as_of else {})})
    missing = ["observed traffic / congestion counts", "bus frequency (GTFS)", "property tax / survey records"]
    return {"layers": rows, "not_available": missing}


def _fmt(v):
    if isinstance(v, float):
        return ("%.3f" % v).rstrip("0").rstrip(".") if abs(v) < 100 else "{:,.0f}".format(v)
    if isinstance(v, int):
        return "{:,}".format(v)
    return str(v)


# 0.4.4 — the ENGINE writes the tables (deterministic, never truncated, exact place labels for the map link); the model
# writes only a short headline + bullets. A reasoning model spent 2,859 of 3,000 tokens thinking and its table was cut
# after the header row (staging, finish=length).
COLS = {   # field → (header, scale) — percentages from fractions; everything else as stored. ORDER = column order.
    "census_density_per_km2": ("Density, census 2011 (/km²)", 1), "census_population": ("People, census 2011", 1),
    "pm25": ("PM2.5 (µg/m³)", 1), "station": ("Station", 1), "reading_at": ("Reading at", 1),
    "density_per_km2": ("Pop. density (/km²)", 1), "population": ("Population", 1),
    "road_density_km_per_km2": ("Road density (km/km²)", 1), "road_length_m": ("Road length (m)", 1),
    "heat_c": ("Heat (°C)", 1), "rainfall_mm": ("Rainfall (mm)", 1), "forecast_peak_precip_mm": ("Forecast peak (mm)", 1),
    "forecast_total_precip_mm": ("Forecast total (mm)", 1), "green_frac": ("Green cover (%)", 100),
    "builtup_frac": ("Built-up (%)", 100), "flood_susceptibility": ("Flood susceptibility", 1),
    "hand_mean_m": ("Height above drainage (m)", 1), "building_coverage": ("Building coverage (%)", 100),
    "mean_height_m": ("Mean height (m)", 1), "built_volume_m3": ("Built volume (m³)", 1),
    "facility_count": ("Facilities", 1),
    "walkability_score": ("Walkability (/100)", 1), "intersection_density_per_km2": ("Junctions (/km²)", 1),
    "transit_gap_score": ("Transit gap (/100)", 1), "dist_bus_stop_m": ("Nearest bus stop (m)", 1),
    "dist_metro_m": ("Nearest metro (m)", 1), "chokepoint_score": ("Chokepoint (/100)", 1),
    "works_count": ("Works", 1), "paid_net_total": ("Paid (₹)", 1),
    "severity": ("Severity", 1), "hotspot_cells": ("Cells in hotspot", 1),
    "underserved_share": ("Far from transit (%)", 100), "paid_per_person": ("Works paid per person (₹)", 1),
    "need_vs_spend": ("Need vs spend (/100)", 1),
    "nearest_km": ("Nearest (km)", 1), "baseline_ha": ("Water 1995–99 (ha)", 1), "recent_ha": ("Water 2017–21 (ha)", 1),
    "water_change_pct": ("Change (%)", 1), "shore_built_1975": ("Shore built-up 1975 (%)", 100), "shore_built_2020": ("Shore built-up 2020 (%)", 100),
    "change_pct": ("Change (%)", 1), "avg_radiance": ("Radiance now", 1), "radiance_first_years": ("Radiance 2014–16", 1),
    "tenders_count": ("Tenders", 1),
    "people_affected": ("People affected", 1), "where": ("Where (top places)", 1), "measure": ("Measure", 1),
    "records": ("Records", 1), "unit": ("Unit", 1), "as_of": ("As of", 1), "what": ("What it is", 1),
}
_SKIP = {"h3", "centroid", "lat_lon", "place", "facilities", "layer", "priority", "caveat", "evidence", "by_year", "title", "metric", "year", "ward"}


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
    pl = facts.get("place") or {}
    if pl.get("profile"):
        rows = [dict(r, _m=r["metric"]) for r in pl["profile"]]
        out.append("**%s — compared with the city median (%s)**\n\n" % (pl.get("label") or pl.get("name"), pl.get("area"))
                   + "\n".join(["| Measure | Here | City median |", "|---|---|---|"]
                                + ["| %s | %s | %s |" % (r["metric"], _num(r["here"]), _num(r["city_median"])) for r in pl["profile"]]))
        if pl.get("facilities_within_1_km"):
            out.append("**Facilities within 1 km**\n\n| Category | Count |\n|---|---|\n" + "\n".join(
                "| %s | %s |" % (k, _num(v)) for k, v in sorted(pl["facilities_within_1_km"].items(), key=lambda x: -x[1])))
    lk = facts.get("lakes") or {}
    if (lk.get("lake_asked") or {}).get("by_year"):
        out.append("**%s — water area by year**\n\n| Year | Water (ha) | Source |\n|---|---|---|\n" % lk["lake_asked"]["place"] + "\n".join(
            "| %s | %s | %s |" % (r["year"], _num(r["water_ha"]), r["source"]) for r in lk["lake_asked"]["by_year"]))
    gr = facts.get("growth") or {}
    if gr.get("city_mean_by_year"):
        out.append("**Night lights — city average by year**\n\n| Year | Mean radiance |\n|---|---|\n" + "\n".join(
            "| %s | %s |" % (r["year"], _num(r["city_mean_radiance"])) for r in gr["city_mean_by_year"]))
    cv = facts.get("civic") or {}
    if cv.get("latest_serious"):
        out.append("**Latest serious civic issues (from news)**\n\n| # | Issue | Category | Severity | Date | Where |\n|---|---|---|---|---|---|\n" + "\n".join(
            "| %d | %s | %s | %s | %s | %s |" % (i, r["title"].replace("|", "/"), r["category"], r["severity"], r["date"], r.get("where") or "—")
            for i, r in enumerate(cv["latest_serious"], 1)))
    pw0 = facts.get("works_trend") or {}
    if pw0.get("works_by_year"):
        out.append("**Works paid by year (city)**\n\n| Year | Works | Paid (₹ crore) |\n|---|---|---|\n" + "\n".join(
            "| %s | %s | %s |" % (r["year"], _num(r["works_count"]), _num(round(r["paid_net_total"] / 1e7, 1))) for r in pw0["works_by_year"]))
    if pw0.get("wards_by_tenders"):
        out.append("**Wards with the most tenders**\n\n" + _table(pw0["wards_by_tenders"], "ward", "Ward"))
    pr = (facts.get("priorities") or {}).get("candidates")
    if pr:
        out.append("**Candidate priorities (ranked by people affected)**\n\n" + _table(pr, "priority", "Priority"))
    inv = (facts.get("inventory") or {}).get("layers")
    if inv:
        out.append("**Data available to the analyst**\n\n" + _table(inv, "layer", "Layer"))
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
                    where = ("cell %s (lon %s, lat %s)" % (r.get("h3"), c[0], c[1])) if c else (
                        "cell %s" % r.get("h3") if r.get("h3") else str(r.get("layer") or r.get("place") or "—"))
                    if r.get("place"):
                        where = "%s — %s" % (r["place"], where)
                    vals = ", ".join("%s %s" % (kk, _fmt(vv)) for kk, vv in r.items() if kk not in ("h3", "centroid", "lat_lon", "place", "layer"))
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


# 0.5.1 — a READ CACHE across questions. The engine module stays loaded in the host, and each question re-read up to ten
# whole datasets (11k profile cells, census, transit, air, facilities…) before the model even started: on staging a
# question took ~60 s, past the public proxy's 60 s timeout (answers came back as an HTML 504 / 'Failed to fetch').
# Reads are kept per (tenant, schema, filter) for KCACHE_TTL; a refused read is never cached. Only with a real tenant.
KCACHE_TTL = float(os.environ.get("ANALYST_CACHE_SECONDS", "900"))
_KCACHE = {}
WARM_SCHEMAS = ("city.cell_profile", "mobility.walkability", "mobility.transit_access", "mobility.chokepoint", "air.hotspot",
                "air.sensor_reading", "demographics.census_ward", "works.ward_cell", "procurement.ward_cell", "geo.facility",
                "water.lake", "water.lake_cell", "econ.nightlight_trend", "civic.issue", "space.place", "space.ward", "space.aoi",
                "governance.recommendation", "city.coverage", "works.city_summary", "works.ward_rollup", "works.contractor_rollup")


def cached_kfind(kfind, tenant):
    if not tenant:
        return kfind
    def f(schema, where=None):
        k = (tenant, schema, json.dumps(where or {}, sort_keys=True))
        hit = _KCACHE.get(k)
        if hit and time.time() - hit[0] < KCACHE_TTL:
            return list(hit[1])
        rows = kfind(schema, where) if where else kfind(schema)
        _KCACHE[k] = (time.time(), rows)
        return list(rows)
    return f


def warm(kfind):
    """Load what the topics read, without the model — so the next real question only waits for the model."""
    t0, n = time.time(), 0
    for sch in WARM_SCHEMAS:
        try:
            n += len(kfind(sch))
        except Exception:
            pass
    try:
        join_air(kfind, {})
    except Exception:
        pass
    return {"answer": "Analyst warmed: %d records in %.1f s" % (n, time.time() - t0), "facts": {"warm": True}, "sources": []}, None


def h_ask(ctx):
    body = ctx.get("body") or {}
    question = (body.get("question") or body.get("q") or "").strip()[:500]
    if not question:
        return None, "question required"
    kfind = cached_kfind(ctx["kfind"], ctx.get("tenant"))
    if question == "__warm__":
        return warm(kfind)

    cov = {}
    for o in kfind("city.coverage"):
        src = o.get("source_schema") or o.get("layer") or o.get("noun")
        if src:
            cov[src] = o
    recs = [o for o in kfind("governance.recommendation")
            if not o.get("status") or o.get("status") in ("open", "proposed", "active")]
    effect = sorted(kfind("intelligence.effectiveness"), key=lambda o: o.get("observed_at") or "", reverse=True)

    topics = topics_of(question)
    place = None
    if topics not in (["inventory"], ["priorities"]) and (PLACE_RE.search(question.lower()) or len(question.split()) <= 4):
        place = detect_place(kfind, question)          # 'tell me about Nilasandra' → a profile of that place
        if place:
            topics = ["place"]
    cells, layer_counts, cell_source = city_cells(kfind)
    used_extra = []
    if any(t in topics for t in ("services", "priorities", "place")):
        fc = load_facilities(kfind)
        if fc:
            layer_counts["geo.facility"] = sum(fc.values()); used_extra.append("geo.facility")
    if any(t in topics for t in ("access", "transit", "walk", "need", "inventory", "priorities", "place")):
        layer_counts.update(join_mobility(kfind, cells))
    air_meta = None
    if any(t in topics for t in ("air", "need", "inventory", "priorities", "place")):
        air_meta = join_air(kfind, cells)
        if air_meta:
            layer_counts["air.quality"] = air_meta["cells"]
    facts = {"question_topics": topics}
    sources, map_cells = [], []
    if cells:
        facts["city_cells"] = {"source": cell_source, "cells": len(cells)}
        for t in topics:
            if t in ("works", "need", "inventory", "priorities", "lakes", "growth", "civic", "place"):
                continue
            tf = air_facts(cells, air_meta) if t == "air" and air_meta else (topic_facts(t, cells, question) if t != "air" else None)
            if tf:
                facts[t] = tf
        sources.append(cell_source.split(" ")[0] if cell_source.startswith("city.") else "raw cell layers")
    if "need" in topics or "priorities" in topics or (place and place.get("ward_no")):
        nf, map_cells, nc = need_facts(kfind, cells)
        if nf:
            facts["need"] = nf
            layer_counts.update(nc)
            sources.append("ward need-vs-spend (census 2011 + works + transit + air)")
            if "priorities" in topics or "place" in topics:   # feeds a candidate / the ward comparison; no full table
                facts.pop("need", None); facts["_need"] = nf; map_cells = []
    if "priorities" in topics:
        try:
            _pl = load_places(kfind)
        except Exception:
            _pl = []
        facts["priorities"] = priorities_facts(kfind, cells, _pl, air_meta, facts.pop("_need", None))
        layer_counts.update({"cell.hydrology": layer_counts.get("cell.hydrology", 0)})
    if "inventory" in topics:
        facts["inventory"] = inventory_facts(kfind, layer_counts, air_meta)
    if any(t in topics for t in ("lakes", "civic", "place")):
        try:
            _places = load_places(kfind)
        except Exception:
            _places = []
    if place:
        pf, pcells = place_facts(place, cells, _places, facts.pop("_need", None))
        if pf:
            facts["place"] = pf; map_cells = pcells; sources.append("place profile")
            used_extra += (["water.lake"] if place.get("lake") else [])
    if "lakes" in topics:
        lf, lake_ids = lake_facts(kfind, question)
        if lf:
            facts["lakes"] = lf; layer_counts["water.lake"] = lf["lakes_measured"]; used_extra.append("water.lake")
            names = {o["id"]: o["name"] for o in _try(kfind, "water.lake") if o.get("id") in lake_ids}
            map_cells = [{"h3": _h3(o), "place": names[o["lake_ref"]]} for o in _try(kfind, "water.lake_cell")
                         if o.get("lake_ref") in names and _h3(o)]
            sources.append("water.lake (Lake Watch)")
    if "growth" in topics:
        gf = growth_facts(kfind)
        if gf:
            facts["growth"] = gf; layer_counts["econ.nightlight_trend"] = gf["cells"]; used_extra.append("econ.nightlight_trend")
            sources.append("econ.nightlight_trend (VIIRS)")
    if "civic" in topics:
        cf = civic_facts(kfind, _places)
        if cf:
            facts["civic"] = cf; layer_counts["civic.issue"] = cf["issues"]; used_extra.append("civic.issue")
            sources.append("civic.issue (news)")
    if "works" in topics:
        wt = works_trend(kfind)
        if wt:
            facts["works_trend"] = wt; used_extra.append("works.ward_cell")
    if "works" in topics or "overview" in topics:      # (not for "need" — its ward table already carries the spend)
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
    facts["layers_used_for_this_question"] = [s for s in wanted if s in layer_counts] + [s for s in dict.fromkeys(used_extra) if s not in wanted]
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
              "grounded in the facts and any open recommendations. If facts.need is present: name the top 2–3 wards, compare the "
              "first with facts.need.city_median_ward (e.g. '3.8× the median density, a third of the median spend per "
              "person'), and say need_vs_spend is an equal-weight ranking aid, not a verdict. If facts.inventory is "
              "present: summarise the data in 3–4 grouped bullets and name what is not available. If facts.place is present: describe "
              "that place in 3–4 bullets against the city median (name what stands out — better or worse), mention facilities "
              "nearby and any lake. If facts.lakes is present: name the lakes that shrank most and say the satellite caveat once. "
              "If facts.growth is present: say how much the city brightened and where, and call night light a PROXY. If "
              "facts.civic is present: say what the serious issues are about and that they come from news. If facts.services "
              "names a category: say how many people are beyond the distance and where (straight line). If facts.priorities is "
              "present: give EXACTLY 5 numbered priorities chosen from facts.priorities.candidates (you may merge or reorder "
              "them, say why in a few words), each ONE line: what, where (from its 'where'), how many people, a first step; "
              "then one line naming facts.priorities.not_ranked; this overrides the 4-bullet limit. Decide quickly — the "
              "candidates are already computed. "
              "Do NOT show reasoning or restate the question. End with "
              "the single line 'Evidence quality: ok' (the full evidence line is appended for you). You inform; officials decide.")
    msgs = [{"role": "system", "content": sysmsg},
            {"role": "user", "content": "FACTS:\n" + json.dumps(facts, default=str)[:12000] + "\n\nQUESTION: " + question}]
    try:
        ans = (ctx["ai_complete"](msgs, PRIORITY_TOKENS if "priorities" in topics else ANSWER_TOKENS) or "").strip()
    except Exception as e:                                             # unavailable / timed out: still answer with the facts
        return {"answer": facts_summary(facts, "is unavailable (%s)" % str(e)[:80]), "sources": sources, "facts": _with_map(facts, map_cells)}, None
    ans = re.sub(r"(?is)<think>.*?</think>", "", ans).strip()
    _tells = ("the user wants", "the facts", "system prompt", "need to", "we need", "let me", "i should")
    if any(t in ans[:120].lower() for t in _tells):
        blocks = [b for b in re.split(r"\n\s*\n", ans) if b.strip()]
        if blocks:
            ans = blocks[-1].strip()
    if not ans.strip():                                                # the model spent its budget reasoning / returned nothing
        if fact_tables(facts):                                         # 0.4.8: the computed tables ARE the answer
            note = ("_The analyst's commentary is unavailable this time (the model used its budget reasoning); the "
                    "computed figures below are complete._\nEvidence quality: ok")
            return {"answer": compose_answer(note, facts), "sources": sources, "facts": _with_map(facts, map_cells)}, None
        return {"answer": facts_summary(facts), "sources": sources, "facts": _with_map(facts, map_cells)}, None
    return {"answer": compose_answer(ans, facts), "sources": sources, "facts": _with_map(facts, map_cells)}, None


def _with_map(facts, map_cells):
    """Every cell of each listed ward, labelled with the ward — the map lights the whole ward on hover. Kept out of the
    FACTS the model sees (thousands of rows), added to the returned facts only."""
    return dict(facts, map_cells=map_cells) if map_cells else facts


HANDLERS = {"city-ask": h_ask}

if __name__ == "__main__":
    print("City Intelligence — the City Scan Analyst. Verb: city-ask (grounded Q&A over governed city layers).")
