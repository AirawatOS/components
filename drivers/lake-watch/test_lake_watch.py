#!/usr/bin/env python3
"""Lake Watch's pure logic: which water bodies get a series, how a year with missing data is handled (a gap, never a
dry lake), and the baseline-vs-recent summary. Exit 1 on failure."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TENANT", "t")
import run as lw

fails = []
def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + str(detail)[:200] if detail else ""))
    if not ok: fails.append(name)

poly = {"type": "Polygon", "coordinates": [[[77.6, 12.9], [77.61, 12.9], [77.61, 12.91], [77.6, 12.9]]]}
F = lambda feat, ha, name=None, g=poly: {"id": "ds:t:geo/osm_feature/way/%d" % int(ha * 10), "feature": feat, "geometry": g,
                                         "osm_type": "way", "osm_id": str(int(ha * 10)),
                                         "tags": dict({"_area_m2": str(int(ha * 1e4))}, **({"name": name} if name else {}))}
picked = lw.pick_lakes([F("lake", 300, "Bellandur Lake"), F("pond", 50), F("wetland", 5, "Some Marsh"), F("reservoir", 0.4),
                        F("water", 12, "Ulsoor Lake"), F("lake", 2, g={"type": "LineString", "coordinates": []})], max_lakes=10, min_ha=1)
check("lakes, reservoirs and NAMED water ≥ 1 ha are measured, largest first; unnamed ponds, tiny ones and non-polygons are not",
      [f["tags"].get("name") for f in picked] == ["Bellandur Lake", "Ulsoor Lake", "Some Marsh"], [f["tags"].get("name") for f in picked])
check("…at most MAX_LAKES", len(lw.pick_lakes([F("lake", 10 + i) for i in range(30)], max_lakes=5)) == 5)

props = {"w1985": 900000, "n1985": 0, "w1986": 880000, "n1986": 600000, "w1987": 860000, "n1987": 0,
         "w2023": 500000, "n2023": 0, "w2024": 480000, "n2024": 0, "w2025": 470000, "n2025": 0}
s = lw.lake_series(props, 1000000)
by = {x["t"]: x for x in s}
check("a year with > MAX_NODATA of the lake unobserved is EMPTY (a gap), with its no-data share kept",
      by["1986"]["water_area_m2"] is None and by["1986"]["nodata_share"] == 0.6, by["1986"])
check("…observed years carry the area and their source (JRC to 2021, Dynamic World after)",
      by["1985"]["water_area_m2"] == 900000 and by["1985"]["source"] == "jrc-gsw" and by["2024"]["source"] == "dynamic-world")
sm = lw.summarise(s, [{"t": "1975", "built_share": 0.1}, {"t": "2020", "built_share": 0.55}], 1000000)
check("baseline = mean of observed 1985–89 years (the gap year excluded); recent = the last 3 observed years",
      sm["baseline_water_m2"] == 880000 and sm["recent_water_m2"] == 483333 and sm["recent_years"] == ["2023", "2024", "2025"], sm)
check("…change in % of the baseline; shore built-up 1975 → 2020 in points", sm["water_change_pct"] == -45.1 and sm["shore_built_change_pts"] == 45.0, sm)
check("…no baseline → no change claimed", lw.summarise(lw.lake_series({"w2024": 1, "n2024": 0}, 10), [], 10)["water_change_pct"] is None)
check("lake id is per tenant + OSM element", lw.lake_id(F("lake", 300, "Bellandur Lake")) == "ds:t:water/lake/way-3000")
print("ALL PASS" if not fails else "FAILED: %d" % len(fails))
sys.exit(1 if fails else 0)
