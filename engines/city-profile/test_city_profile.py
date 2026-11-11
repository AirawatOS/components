#!/usr/bin/env python3
"""Behavioural check — City Profile joins the baseline layers per cell, never invents a value, never crosses tenants.

Drives the real build_profiles() with in-memory layers shaped exactly as the drivers write them (cell_ref / space /
flat h3_id), and checks:
  • one city.cell_profile per cell present in ANY layer, with each layer's value under its profile field;
  • a layer the tenant has NOT built is null in the profile and absent from layers_present (never filled in or zeroed);
  • another tenant's records are ignored;
  • facilities are counted per category per cell;
  • every profile records its source layers with observation times, and the governed schema accepts the record shape.
Exit 1 on failure.
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run as cp                    # noqa: E402
import govern_city_profile as gov   # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + str(detail)[:180] if detail else ""))
    if not ok:
        fails.append(name)


T, A, B = "janaagraha", "89618927647ffff", "8960145366fffff"
OBS = "2026-11-11T11:15:00Z"
DATA = {
    "cell.heat": [{"id": "ds:janaagraha:cell/heat/" + A, "cell_ref": {"h3_id": A, "centroid": [77.5, 12.9]}, "value": 28.1,
                   "prov": {"observed_at": OBS}},
                  {"id": "ds:janaagraha:cell/heat/" + B, "cell_ref": {"h3_id": B}, "value": 31.4, "observed_at": OBS},
                  {"id": "ds:othercity:cell/heat/" + A, "cell_ref": {"h3_id": A}, "value": 99.0}],          # another tenant
    "cell.rainfall": [{"id": "ds:janaagraha:cell/rainfall/" + A, "cell_ref": {"h3_id": A}, "rainfall_mm": 912.0,
                       "observed_at": "2025-12-31T00:00:00Z"}],
    "demographics.population": [{"id": "ds:janaagraha:demographics/population/" + A, "space": {"h3": A},
                                 "population": 1450.5, "density_per_km2": 13200.0, "observed_at": "2020-01-01T00:00:00Z"}],
    "cell.roads": [{"id": "ds:janaagraha:cell/roads/" + B, "cell_ref": {"h3_id": B}, "road_length_m": 4200.0,
                    "road_density_km_per_km2": 38.2}],
    "geo.facility": [{"id": "ds:janaagraha:facility/health/n1", "h3_id": A, "category": "health"},
                     {"id": "ds:janaagraha:facility/health/n2", "h3_id": A, "category": "health"},
                     {"id": "ds:janaagraha:facility/school/n3", "h3_id": A, "category": "education"}],
    # cell.buildings, cell.hydrology, … deliberately NOT built for this tenant
}
prof, counts = cp.build_profiles(lambda s: DATA.get(s, []), T, "ds:janaagraha:space/aoi/bengaluru", "2026-11-11T12:00:00Z")
pa, pb = prof.get(A, {}), prof.get(B, {})

check("one profile per cell present in any layer (2 cells)", sorted(prof) == sorted([A, B]), sorted(prof))
check("cell A joins heat, rainfall, population (from three record shapes)",
      (pa.get("heat_c"), pa.get("rainfall_mm"), pa.get("population"), pa.get("density_per_km2")) == (28.1, 912.0, 1450.5, 13200.0), pa)
check("cell B joins heat and roads", (pb.get("heat_c"), pb.get("road_density_km_per_km2"), pb.get("road_length_m")) == (31.4, 38.2, 4200.0), pb)
check("another tenant's record is ignored (heat 99.0 never appears)", pa.get("heat_c") != 99.0 and counts["cell.heat"] == 2, counts)
check("a layer not built is NULL, not zero or estimated (buildings, hydrology)",
      all(pa.get(f) is None for f in ("building_coverage", "flood_susceptibility", "hand_mean_m")) and pb.get("population") is None)
check("layers_present lists exactly the layers that supplied the cell",
      pa.get("layers_present") == ["cell.heat", "cell.rainfall", "demographics.population", "geo.facility"]
      and pb.get("layers_present") == ["cell.heat", "cell.roads"], (pa.get("layers_present"), pb.get("layers_present")))
check("observation time kept per source layer", pa["layers"]["cell.heat"] == OBS and pa["layers"]["cell.rainfall"] == "2025-12-31T00:00:00Z")
check("facilities counted per category", pa.get("facilities") == {"health": 2, "education": 1} and pa.get("facility_count") == 3, pa.get("facilities"))
check("ids are the tenant's own, deterministic", pa.get("id") == "ds:janaagraha:city/cell_profile/" + A)
check("centroid carried from the layer record", pa["cell_ref"].get("centroid") == [77.5, 12.9])

try:
    import jsonschema
    jsonschema.validate(pa, gov.defn()); jsonschema.validate(pb, gov.defn()); ok, why = True, ""
except ImportError:
    d = gov.defn(); extra = set(pa) - set(d["properties"]); ok, why = not extra and all(k in pa for k in d["required"]), extra
except Exception as e:
    ok, why = False, str(e)[:160]
check("the governed schema accepts the profile records", ok, why)

print("ALL PASS" if not fails else "FAILED: %d" % len(fails))
sys.exit(1 if fails else 0)
