#!/usr/bin/env python3
"""Census Ward: each cell of a ward carries that ward's 2011 census population, area and density; cells of wards
without census figures get nothing; ids are deterministic. Exit 1 on failure."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
os.environ["TENANT"] = "t"
import run as cw   # noqa: E402
fails = []
def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + str(detail)[:200] if detail else ""))
    if not ok: fails.append(name)
ward_of = {"a": "120", "b": "120", "c": "135", "x": "999"}
census = {"120": {"pop": 25000.0, "area_km2": 0.5, "name": "Cottonpete"}, "135": {"pop": 35213.0, "area_km2": 0.31, "name": "Padarayanapura"}}
R = {r["cell_ref"]["h3_id"]: r for r in cw.census_cells(ward_of, census, {"a": [77.56, 12.97]}, "now")}
check("every cell of a ward carries its census population and density", R["a"]["census_population"] == 25000 and R["b"]["census_density_per_km2"] == 50000)
check("densest-ward arithmetic (Padarayanapura 35,213 / 0.31 km²)", R["c"]["census_density_per_km2"] == 113590, R["c"])
check("a ward with no census figures gets no record", "x" not in R)
check("ids deterministic + centroid carried", R["a"]["id"] == "ds:t:demographics/census_ward/a" and R["a"]["cell_ref"]["centroid"] == [77.56, 12.97])
check("ward_no parsing ('150.0', 'Ward 7', 12)", (cw.ward_no("150.0"), cw.ward_no("Ward 7"), cw.ward_no(12)) == ("150", "7", "12"))
print("ALL PASS" if not fails else "FAILED: %d" % len(fails)); sys.exit(1 if fails else 0)
