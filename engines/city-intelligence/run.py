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
import collections, json, math, os, re

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
    hp = collections.defaultdict(list)
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
    ("geo.facility", "Facilities (cells with one)", "cells", "health, schools, police, government…"),
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


NO_AS_OF = {"works.ward_cell", "procurement.ward_cell", "space.place"}   # their observed_at is the build time, not the data's


def inventory_facts(kfind, layer_counts, air_meta):
    rows = []
    for schema, name, unit, what in INVENTORY:
        if schema == "air.quality" and air_meta:
            n, as_of = air_meta["cells"], air_meta["observed_at"][:16].replace("T", " ") + " UTC"
        elif schema in layer_counts:
            n, as_of = layer_counts[schema], None
        else:
            rs = _try(kfind, schema)
            n = len(rs)
            ts = [o.get("observed_at") for o in rs if isinstance(o.get("observed_at"), str)]
            as_of = (max(ts)[:10] if ts and schema not in NO_AS_OF else None)
        if n:
            rows.append({"layer": name, "records": n, "unit": unit, "what": what, **({"as_of": as_of} if as_of else {})})
    missing = ["observed traffic / congestion counts", "satellite aerosol (AOD) and night lights (not yet run)",
               "property tax / survey records"]
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
    "people_affected": ("People affected", 1), "where": ("Where (top places)", 1), "measure": ("Measure", 1),
    "records": ("Records", 1), "unit": ("Unit", 1), "as_of": ("As of", 1), "what": ("What it is", 1),
}
_SKIP = {"h3", "centroid", "lat_lon", "place", "facilities", "layer", "priority", "caveat", "evidence"}


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
    if any(t in topics for t in ("access", "transit", "walk", "need", "inventory", "priorities")):
        layer_counts.update(join_mobility(kfind, cells))
    air_meta = None
    if any(t in topics for t in ("air", "need", "inventory", "priorities")):
        air_meta = join_air(kfind, cells)
        if air_meta:
            layer_counts["air.quality"] = air_meta["cells"]
    facts = {"question_topics": topics}
    sources, map_cells = [], []
    if cells:
        facts["city_cells"] = {"source": cell_source, "cells": len(cells)}
        for t in topics:
            if t in ("works", "need", "inventory", "priorities"):
                continue
            tf = air_facts(cells, air_meta) if t == "air" and air_meta else (topic_facts(t, cells, question) if t != "air" else None)
            if tf:
                facts[t] = tf
        sources.append(cell_source.split(" ")[0] if cell_source.startswith("city.") else "raw cell layers")
    if "need" in topics or "priorities" in topics:
        nf, map_cells, nc = need_facts(kfind, cells)
        if nf:
            facts["need"] = nf
            layer_counts.update(nc)
            sources.append("ward need-vs-spend (census 2011 + works + transit + air)")
            if "priorities" in topics:                 # the ward list feeds a candidate; its full table is not shown
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
              "grounded in the facts and any open recommendations. If facts.need is present: name the top 2–3 wards, compare the "
              "first with facts.need.city_median_ward (e.g. '3.8× the median density, a third of the median spend per "
              "person'), and say need_vs_spend is an equal-weight ranking aid, not a verdict. If facts.inventory is "
              "present: summarise the data in 3–4 grouped bullets and name what is not available. If facts.priorities is "
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
