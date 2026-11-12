#!/usr/bin/env python3
"""Behavioural check — City Intelligence 0.4.x grounds Ask on the city's REAL layers, says what is missing, labels proxies.

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

def ask_empty_model(question, data):
    seen = {}
    def model(msgs, n):
        seen["n"] = n
        return ""                                       # the model spent its whole budget reasoning (finish=length)
    out, err = ci.h_ask({"body": {"question": question}, "kfind": lambda s, where=None: list(data.get(s, [])), "ai_complete": model})
    return out, err, seen.get("n")


out, err, n = ask_empty_model("where are the hottest areas and how many people live there?", BASE)
ans = (out or {}).get("answer") or ""
check("the model gets an answer budget of 1800 tokens (900 left a reasoning model nothing to answer with)", n == 1800, n)
check("an empty model reply never becomes 'No answer' — the computed facts are shown instead, numbers included",
      ans.startswith("The analyst model returned no text") and "cell a (lon 77.5, lat 12.9)" in ans and "heat_c 33.5" in ans and "population 12,000" in ans, ans[:300])
check("…and it still ends with the evidence-quality line", "Evidence quality:" in ans and "city.cell_profile" in ans, ans[-160:])

def boom(msgs, n):
    raise TimeoutError("timed out")
out, err = ci.h_ask({"body": {"question": "hottest areas?"}, "kfind": lambda s, where=None: list(BASE.get(s, [])), "ai_complete": boom})
check("a model timeout still answers with the computed facts (and says why)",
      not err and (out or {}).get("answer", "").startswith("The analyst model is unavailable (timed out)") and "heat_c 33.5" in out["answer"], (err, (out or {}).get("answer", "")[:120]))

# 0.4.2 — staging: an outskirts cell (1,607/km² on ~0.19 km/km² of road) topped the "congestion" proxy over the dense core
OUT = dict(BASE, **{"city.cell_profile": PROFILES + [
    profile("o", 77.78, 12.84, population=300, density_per_km2=1607, road_density_km_per_km2=0.12)]})
out, err, f, sysmsg = ask("which areas are most congested?", OUT)
acc = f.get("access") or {}
prox = acc.get(next((k for k in acc if k.startswith("PROXY")), ""), [])
check("0.4.2 proxy: a sparse outskirts cell with almost no roads does not top it (only the denser half competes)",
      prox and prox[0].get("h3") == "a" and "o" not in [r.get("h3") for r in prox], [r.get("h3") for r in prox])
check("0.4.2 rows carry a ready-to-print 'lat, lon' (centroid is [lon, lat])", prox and prox[0].get("lat_lon") == "12.9000, 77.5000", prox[:1])
check("0.4.4 the model writes prose only (no tables — the engine appends them), never h3 ids",
      "Do NOT write any table" in sysmsg and "never print h3 ids" in sysmsg)

# 0.4.3 — cells are named by their nearest gazetteer place, with the distance; far cells keep lat/lon only
GAZ = dict(BASE, **{"space.place": [
    {"kind": "locality", "name": "Jayanagar", "centroid": [77.5005, 12.9010]},      # ~0.1 km from cell a
    {"kind": "locality", "name": "Hebbal", "centroid": [77.5950, 12.9600]},         # ~1.2 km from cell b
    {"kind": "pincode", "name": "560011", "centroid": [77.7000, 13.0000]},           # a pincode is not a name
]})
out, err, f, sysmsg = ask("what are the hottest areas?", GAZ)
hot = {r["h3"]: r for r in (f.get("heat") or {}).get("hottest_cells", [])}
check("0.4.3 a cell next to a named place carries it with the distance", hot.get("a", {}).get("place") == "Jayanagar (0.1 km)", hot.get("a"))
check("…a cell 1–2.5 km away is 'near' the place", hot.get("b", {}).get("place") == "near Hebbal (1.2 km)", hot.get("b"))
check("…a cell with no named place within 2.5 km gets none (pincodes are not names)", "place" not in hot.get("c", {}), hot.get("c"))
check("…the gazetteer is cited and the model is told to keep 'near' and the distance",
      "space.place" in (out or {}).get("sources", []) and "keep the distance and the word 'near'" in sysmsg)
out, err, f, _ = ask("what are the hottest areas?", BASE)
check("…no gazetteer → rows unchanged (lat/lon only), nothing claimed", all("place" not in r for r in (f.get("heat") or {}).get("hottest_cells", [])))

# 0.4.4 — staging: the reasoning model spent 2,859 of 3,000 tokens thinking; its table stopped after the header row.
def ask_text(question, data, reply):
    out, err = ci.h_ask({"body": {"question": question}, "kfind": lambda s, where=None: list(data.get(s, [])),
                         "ai_complete": lambda msgs, n: reply})
    return (out or {}).get("answer") or "", err

PLACED = dict(OUT, **{"space.place": [{"kind": "locality", "name": "Nagamma Nagar", "centroid": [77.5003, 12.9008]}]})
ans, err = ask_text("which areas are most congested?", PLACED, "**Congestion is not measured.**\n- Proxy only.\nEvidence quality: ok")
rows = [l for l in ans.splitlines() if l.startswith("| ") and not l.startswith("| #")]
check("0.4.4 the engine appends the ranked table itself, every computed row, with the exact place label",
      len(rows) == 2 and rows[0] == "| 1 | Nagamma Nagar (0.1 km) | 48,000 | 4 |" and "| Pop. density (/km²) | Road density (km/km²) |" in ans, rows)
check("…prose first, then the table, then ONE engine evidence line (the model's placeholder dropped)",
      ans.startswith("**Congestion is not measured.**") and ans.count("Evidence quality:") == 1 and ans.rstrip().endswith(".")
      and "layers used: cell.roads" in ans and "place names: OpenStreetMap gazetteer" in ans, ans[-200:])
ans, err = ask_text("which areas are most congested?", PLACED,
                    "**Congestion cannot be ranked directly.**\n- Road density covers 4 cells.\nCells supplied for the proxy:\n\n| # | Location | Pop. density (/km²) |\n|---|---|---|\n| 1 | Nagamma Na")
check("…a CUT-OFF reply: its dangling table is dropped, it is marked shortened, and the full table + evidence still follow",
      "Nagamma Na |" not in ans and "| 1 | Nagamma Na\n" not in ans and "cut short" in ans and len([l for l in ans.splitlines() if l.startswith("| 1 |")]) == 1
      and "Evidence quality:" in ans, ans)
ans, err = ask_text("which wards spent the most on works?", BASE, "**Ward 7 leads.**\nEvidence quality: ok")
check("…works rollups become Ward / Contractor tables", "| # | Ward | Works | Paid (₹) |" in ans and "| 1 | Ward 7 | 1,200 | 610,000,000 |" in ans
      and "| 1 | Acme Roads |" in ans, ans)

# 0.4.5 — a GHSL density above the densest census ward (staging: Cottonpete cell 197,759 /km²) is not ranked on density
SPIKE = dict(OUT, **{"city.cell_profile": OUT["city.cell_profile"] + [
    profile("s", 77.5638, 12.9722, population=21446, density_per_km2=197759, road_density_km_per_km2=35.7)],
    "space.place": [{"kind": "locality", "name": "Nagamma Nagar", "centroid": [77.5641, 12.9725]}]})
out, err, f, _ = ask("which areas are most congested?", SPIKE)
acc = f.get("access") or {}
prox = acc.get(next((k for k in acc if k.startswith("PROXY")), ""), [])
ex = f.get("excluded_as_implausible") or {}
check("0.4.5 the implausible spike is NOT ranked in the congestion proxy", "s" not in [r.get("h3") for r in prox], [r.get("h3") for r in prox])
check("…it is listed (with its place) under excluded_as_implausible, with the reason",
      [r.get("h3") for r in ex.get("cells", [])] == ["s"] and ex["cells"][0].get("place", "").startswith("Nagamma Nagar") and "census" in ex.get("why", ""), ex)
ans, err = ask_text("which areas are most congested?", SPIKE, "**x**\nEvidence quality: ok")
check("…and the answer carries a 'Not ranked — modelled density looks implausible' table", "Not ranked — modelled density looks implausible" in ans and "Nagamma Nagar" in ans)
out, err, f, _ = ask("which areas are most populated?", SPIKE)
check("…and is not in the most-populous list either", "s" not in [r.get("h3") for r in (f.get("population") or {}).get("most_populous_cells", [])])

# 0.4.6 — mobility layers (driver/mobility-indexes) joined by h3
MOB = dict(BASE, **{
    "mobility.walkability": [{"cell_ref": {"h3_id": "a"}, "walkability_score": 12.0, "intersection_density_per_km2": 40.0},
                             {"cell_ref": {"h3_id": "b"}, "walkability_score": 88.0, "intersection_density_per_km2": 210.0},
                             {"cell_ref": {"h3_id": "c"}, "walkability_score": 50.0, "intersection_density_per_km2": 120.0}],
    "mobility.transit_access": [{"cell_ref": {"h3_id": "a"}, "transit_gap_score": 95.0, "dist_bus_stop_m": 2100, "dist_metro_m": 5200},
                                {"cell_ref": {"h3_id": "b"}, "transit_gap_score": 0.0, "dist_bus_stop_m": 120, "dist_metro_m": 800},
                                {"cell_ref": {"h3_id": "c"}, "transit_gap_score": 40.0, "dist_bus_stop_m": 900, "dist_metro_m": 6000}],
    "mobility.chokepoint": [{"cell_ref": {"h3_id": "b"}, "chokepoint_score": 100.0}, {"cell_ref": {"h3_id": "c"}, "chokepoint_score": 35.0}]})
out, err, f, _ = ask("where is bus and metro access worst?", MOB)
tr = f.get("transit") or {}
gaps = tr.get(next((k for k in tr if k.startswith("worst_transit_gaps")), ""), [])
check("0.4.6 transit question → worst gaps first (a, then c); a cell with no gap is not listed",
      [r["h3"] for r in gaps] == ["a", "c"] and gaps[0]["dist_bus_stop_m"] == 2100, [r.get("h3") for r in gaps])
check("…underserved cells and people counted", tr.get("underserved_cells") == 2 and tr.get("people_in_underserved_cells") == 12500, tr)
check("…the transit layer counts as used evidence", "mobility.transit_access" in f.get("layers_used_for_this_question", []), f.get("layers_used_for_this_question"))
out, err, f, _ = ask("which areas are least walkable?", MOB)
wk = f.get("walk") or {}
least = wk.get(next((k for k in wk if k.startswith("least_walkable")), ""), [])
check("…walkability question → least walkable POPULATED cells first (a; c has < 2,000 people)", [r["h3"] for r in least] == ["a", "b"], [r.get("h3") for r in least])
out, err, f, _ = ask("which areas are most congested?", MOB)
acc = f.get("access") or {}
ck = acc.get(next((k for k in acc if "chokepoint" in k), ""), [])
check("…congestion question also gets the chokepoint PROXY (b first), labelled not observed traffic",
      [r["h3"] for r in ck][:1] == ["b"] and any("NOT observed traffic" in k for k in acc), list(acc))
ans, err = ask_text("where is bus and metro access worst?", MOB, "**x**\nEvidence quality: ok")
check("…tables carry mobility column headers", "Transit gap (/100)" in ans and "Nearest bus stop (m)" in ans, ans[:300])
out, err, f, _ = ask("where is bus and metro access worst?", BASE)
check("…with no mobility layers built, the transit layer is reported MISSING (not an all-clear)",
      "mobility.transit_access" in f.get("layers_missing_for_this_question", []), f.get("layers_missing_for_this_question"))

def refusing(s, where=None):
    if s.startswith("mobility."): raise PermissionError("403 no read grant")
    return list(MOB.get(s, []))
out, err = ci.h_ask({"body": {"question": "where is bus access worst?"}, "kfind": refusing, "ai_complete": lambda m, n: "x\nEvidence quality: ok"})
check("…a refused mobility read (stale grant) → reported missing, the answer still comes back",
      not err and "mobility.transit_access" in (out or {}).get("facts", {}).get("layers_missing_for_this_question", []), err)

# 0.4.7 — the data inventory, air quality, and the ward need-vs-spend ranking
check("0.4.7 'list the data you have access to' → the INVENTORY only (not the access/congestion topic)",
      ci.topics_of("List the data you have access to") == ["inventory"] and ci.topics_of("what data do you have?") == ["inventory"],
      ci.topics_of("List the data you have access to"))
check("…'hotspot' is air, not heat; 'airport' is not air", ci.topics_of("where are the pollution hotspots?") == ["air"]
      and "air" not in ci.topics_of("roads near the airport"), (ci.topics_of("where are the pollution hotspots?"), ci.topics_of("roads near the airport")))
check("…works + people / metro → the need-vs-spend ranking alone",
      ci.topics_of("Which crowded wards have weak metro access and low works spending?") == ["need"]
      and ci.topics_of("works spend per person by ward") == ["need"] and ci.topics_of("which ward got the most works spend?") == ["works"],
      ci.topics_of("Which crowded wards have weak metro access and low works spending?"))
H = "2026-11-12T16:30:00Z"
def aq(h, v, hour=H): return {"schema": "air.quality", "space": {"h3": h, "centroid": [77.6, 12.9]}, "pm25": v, "observed_at": hour, "method": "idw"}
AIR = dict(MOB, **{
    "air.hotspot": [{"space": {"h3": "a", "centroid": [77.50, 12.90]}, "value": 66.2, "severity": "info", "cell_count": 40, "observed_at": H}],
    "air.sensor_reading": [{"space": {"h3": "b", "centroid": [77.60, 12.95]}, "sensor_id": "cpcb-jayanagar-5th-block--bengaluru---kspcb", "pm25": 91.5, "observed_at": "2026-11-12T17:30:00Z"}],
    # the CURRENT hour (50+ cells so it is not a partial run) and an OLD, superseded hour with absurd values
    "air.quality": [aq("a", 60.0), aq("b", 50.0), aq("c", 40.0)] + [aq("x%d" % i, 45.0) for i in range(60)]
                   + [aq("a", 400.0, "2026-11-11T17:30:00Z"), aq("b", 380.0, "2026-11-11T17:30:00Z")],
    "demographics.census_ward": [
        {"ward_no": "116", "ward_name": "Nilasandra", "census_population": 30000, "census_density_per_km2": 67000, "cell_ref": {"h3_id": "a"}},
        {"ward_no": "7", "ward_name": "Leafy", "census_population": 30000, "census_density_per_km2": 9000, "cell_ref": {"h3_id": "b"}},
        {"ward_no": "9", "ward_name": "Outer", "census_population": 5000, "census_density_per_km2": 2000, "cell_ref": {"h3_id": "c"}}],
    "works.ward_cell": [{"ward_no": "116", "paid_net_total": 1.5e8, "works_count": 70, "cell_ref": {"h3_id": "a"}, "series": [{"t": "2012"}, {"t": "2022"}]},
                        {"ward_no": "7", "paid_net_total": 9.0e8, "works_count": 300, "cell_ref": {"h3_id": "b"}},
                        {"ward_no": "9", "paid_net_total": 2.0e8, "works_count": 50, "cell_ref": {"h3_id": "c"}}],
    "news.item": [{"title": "n1"}, {"title": "n2"}]})
out, err, f, _ = ask("where is air pollution worst?", AIR)
a_ = f.get("air") or {}
check("…air question → the LATEST hour only (the stale 400 µg/m³ hour never appears)",
      a_.get("pm25_hour") == H and (a_.get("pm25") or {}).get("max") == 60.0, a_.get("pm25"))
check("…most polluted first, hotspots and the measured stations listed, and it says it is an estimate",
      a_["most_polluted_cells"][0]["h3"] == "a" and a_["hotspots (top 5% of the city surface)"][0]["severity"] == "info"
      and a_["stations (measured)"][0]["station"] == "Jayanagar 5th Block" and "ESTIMATE" in a_.get("how_estimated", ""), a_.get("stations (measured)"))
check("…air.quality counts as used evidence", "air.quality" in f.get("layers_used_for_this_question", []), f.get("layers_used_for_this_question"))
out, err, f, _ = ask("Which crowded wards have weak metro access and low works spending per person?", AIR)
nd = f.get("need") or {}
rows = nd.get("wards_with_most_need_for_least_spend") or []
check("…need question → the dense, polluted, far-from-metro, under-spent ward ranks first",
      [r["place"] for r in rows][:1] == ["Nilasandra (ward 116)"] and rows[0]["paid_per_person"] == 5000.0 and rows[0]["need_vs_spend"] >= rows[-1]["need_vs_spend"],
      [(r["place"], r.get("need_vs_spend")) for r in rows])
check("…it states the period, the city median ward and that it is a ranking aid",
      nd.get("works_period") == "2012–2022" and nd["city_median_ward"]["paid_per_person"] and "RANKING AID" in nd.get("how_ranked", ""), nd.get("city_median_ward"))
check("…no generic works / transit tables are attached to it", "public_works" not in f and "transit" not in f, list(f))
check("…the ward's map cells are NOT sent to the model", "map_cells" not in f)
check("…but are returned for the map (every cell labelled with its ward)",
      {"h3": "a", "place": "Nilasandra (ward 116)"} in (out or {}).get("facts", {}).get("map_cells", []), (out or {}).get("facts", {}).get("map_cells"))
ans, err = ask_text("Which crowded wards have weak metro access and low works spending per person?", AIR, "**x**\nEvidence quality: ok")
check("…the answer carries the ward table with its headers",
      "Nilasandra (ward 116)" in ans and "Works paid per person (₹)" in ans and "Need vs spend (/100)" in ans, ans[:400])
ans, err = ask_text("List the data you have access to", AIR, "**x**\nEvidence quality: ok")
check("…the inventory answer is ONE data table — air, census, works and news included, no congestion proxy",
      "Data available to the analyst" in ans and "Census 2011 by ward" in ans and "Air quality — PM2.5 surface" in ans
      and "News articles" in ans and "Proxy" not in ans, ans[:500])

out, err, f, _ = ask("", BASE)
check("an empty question is refused", err == "question required")

print("ALL PASS" if not fails else "FAILED: %d" % len(fails))
sys.exit(1 if fails else 0)
