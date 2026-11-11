#!/usr/bin/env python3
"""Behavioural check — City Intelligence 0.4.0 grounds Ask on the city's REAL layers, says what is missing, labels proxies.

Drives the real h_ask() with an in-memory kfind (records shaped as the drivers / rollups write them) and a stub model
that captures the FACTS it is grounded on (the model never computes a figure, so the facts are what matters):
  • "most congested?" → the access topic: NO traffic layer is stated as missing, the answer is a labelled PROXY
    (population density ÷ road density), never presented as congestion;
  • "where to add health centres?" → populous cells with no health facility, ranked by population;
  • "hottest areas?" → hottest cells and heat × population exposure, from the cell profile;
  • a layer the tenant has not built is listed in layers_missing_for_this_question (never an all-clear);
  • works questions use the ward / contractor rollups; ALL open recommendations are included (not only flood/crowd);
  • with NO city.cell_profile, the raw cell layers are joined instead and the fallback is said;
  • the mass-gathering facts appear only when risk.flood / crowd / CCTV data exists (0.3.0 behaviour kept);
  • the model is told to use ONLY the facts and to call proxies proxies.
Exit 1 on failure.
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run as ci   # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + str(detail)[:200] if detail else ""))
    if not ok:
        fails.append(name)


def profile(h, lon, lat, **v):
    p = {"schema": "city.cell_profile", "id": "ds:t:city/cell_profile/" + h, "cell_ref": {"h3_id": h, "centroid": [lon, lat]}}
    p.update(v)
    layers = sorted({ci.FIELD_LAYER[k] for k in v if k in ci.FIELD_LAYER} | ({"geo.facility"} if "facilities" in v else set()))
    p["layers_present"] = layers
    return p


PROFILES = [
    profile("a", 77.50, 12.90, heat_c=33.5, population=12000, density_per_km2=48000, road_density_km_per_km2=4.0,
            facility_count=0, green_frac=0.05, builtup_frac=0.9),
    profile("b", 77.60, 12.95, heat_c=29.0, population=9000, density_per_km2=36000, road_density_km_per_km2=30.0,
            facility_count=2, facilities={"health": 1, "education": 1}, green_frac=0.30, builtup_frac=0.6),
    profile("c", 77.70, 13.00, heat_c=31.0, population=500, density_per_km2=2000, road_density_km_per_km2=2.0,
            facility_count=1, facilities={"education": 1}, green_frac=0.70, builtup_frac=0.2),
]
BASE = {
    "city.cell_profile": PROFILES,
    "works.city_summary": [{"works_count": 40733, "wards_count": 198, "contractors_count": 12089, "paid_net_total": 9.1e9}],
    "works.ward_rollup": [{"ward": "Ward 12", "works_count": 900, "paid_net_total": 4.2e8},
                          {"ward": "Ward 7", "works_count": 1200, "paid_net_total": 6.1e8}],
    "works.contractor_rollup": [{"contractor": "k1", "name": "Acme Roads", "works_count": 310, "paid_net_total": 2.0e8}],
    "governance.recommendation": [{"title": "Contractor concentration in Ward 7", "proposed_action": "review awards",
                                   "rationale": "one contractor holds 40% of value"},
                                  {"title": "Closed one", "status": "rejected"}],
    "city.coverage": [{"source_schema": "cell.heat", "coverage_pct": 100.0}],
}


def ask(question, data):
    seen = {}

    def model(msgs, n):
        seen["system"] = msgs[0]["content"]
        seen["facts"] = json.loads(msgs[1]["content"].split("FACTS:\n", 1)[1].split("\n\nQUESTION:", 1)[0])
        return "Headline.\n- bullet\nEvidence quality: test"

    out, err = ci.h_ask({"body": {"question": question}, "kfind": lambda s, where=None: list(data.get(s, [])), "ai_complete": model})
    return out, err, seen.get("facts") or {}, seen.get("system") or ""


out, err, f, sysmsg = ask("which areas are most congested?", BASE)
acc = f.get("access") or {}
proxy_key = next((k for k in acc if k.startswith("PROXY")), "")
check("congestion → access topic, answered and grounded", not err and "access" in f.get("question_topics", []), err)
check("…no traffic layer is stated as MISSING", any("traffic" in m for m in f.get("layers_missing_for_this_question", [])),
      f.get("layers_missing_for_this_question"))
check("…the ranking is a labelled PROXY, not 'congestion'", proxy_key and "NOT congestion" in proxy_key and acc.get("traffic_or_congestion_layer_present") is False, proxy_key)
check("…proxy ranks dense people on sparse roads first (cell a)", (acc.get(proxy_key) or [{}])[0].get("h3") == "a", acc.get(proxy_key))
check("…the model is told proxies are proxies and to use only the facts", "PROXY" in sysmsg and "ONLY the FACTS" in sysmsg)

out, err, f, _ = ask("where should we add healthcare centres?", BASE)
svc = f.get("services") or {}
gap_key = next((k for k in svc if k.startswith("populous_cells_without")), "")
gaps = [r["h3"] for r in (svc.get(gap_key) or [])]
check("health centres → populous cells WITHOUT a health facility, by population (a before c; b has one)",
      gaps == ["a", "c"] and svc.get("category_asked") == ["health"], (gaps, svc.get("category_asked")))

out, err, f, _ = ask("what are the hottest areas?", BASE)
heat = f.get("heat") or {}
check("heat → hottest cells from the profile (a first)", (heat.get("hottest_cells") or [{}])[0].get("h3") == "a", heat.get("hottest_cells"))
check("…and heat × population exposure", any(k.startswith("heat_exposure") for k in heat))
check("…source is the city.cell_profile rollup", (f.get("city_cells") or {}).get("source") == "city.cell_profile")

out, err, f, _ = ask("where is flooding likely?", BASE)
check("flood question with no hydrology layer built → hydrology listed as MISSING (not an all-clear)",
      "cell.hydrology" in f.get("layers_missing_for_this_question", []), f.get("layers_missing_for_this_question"))

out, err, f, _ = ask("which wards spent the most on works?", BASE)
pw = f.get("public_works") or {}
check("works → ward and contractor rollups, wards ordered by spend",
      [w["ward"] for w in pw.get("wards_by_spend", [])] == ["Ward 7", "Ward 12"] and pw.get("contractors_by_spend"), pw)
check("ALL open recommendations included (not only flood/crowd); a rejected one is not",
      [r["title"] for r in f.get("open_recommendations", [])] == ["Contractor concentration in Ward 7"], f.get("open_recommendations"))
check("no mass-gathering facts when those layers are absent", "mass_gathering" not in f)

RAW = {k: v for k, v in BASE.items() if k != "city.cell_profile"}
RAW["cell.heat"] = [{"cell_ref": {"h3_id": "x", "centroid": [77.1, 12.1]}, "value": 35.0},
                    {"cell_ref": {"h3_id": "y", "centroid": [77.2, 12.2]}, "value": 30.0}]
RAW["demographics.population"] = [{"space": {"h3": "x"}, "population": 800}, {"space": {"h3": "y"}, "population": 5000}]
out, err, f, _ = ask("what are the hottest areas?", RAW)
check("no profile → falls back to the raw cell layers, and says so",
      "raw layers" in ((f.get("city_cells") or {}).get("source") or "") and (f.get("heat") or {}).get("hottest_cells", [{}])[0].get("h3") == "x",
      f.get("city_cells"))

CROWD = dict(BASE, **{"risk.flood": [{"space": {"h3": "g"}, "level": "high", "score": 0.9}],
                      "cell.crowd_density": [{"space": {"h3": "g"}, "headcount": 4000, "band": "critical", "density_sqm": 5.2}]})
out, err, f, _ = ask("where are people at risk?", CROWD)
mg = f.get("mass_gathering") or {}
check("mass-gathering facts kept when flood + crowd data exist (0.3.0 behaviour)",
      mg.get("flood_and_crowd_overlap_cells") == 1 and mg.get("people_exposed_in_overlap") == 4000, mg)

out, err, f, _ = ask("", BASE)
check("an empty question is refused", err == "question required")

print("ALL PASS" if not fails else "FAILED: %d" % len(fails))
sys.exit(1 if fails else 0)
