#!/usr/bin/env python3
"""osm-facilities 0.2.0: the Public Services / Points of Interest taxonomy — new types land in the right category and
existing keys are unchanged (the analyst and city profile read them). Exit 1 on failure."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TENANT", "t")
import run as f
fails = []
def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + str(detail) if detail else ""))
    if not ok: fails.append(name)
M = f._match
check("existing keys unchanged (hospital → healthcare, school → education, police → emergency)",
      M({"amenity": "hospital"}) == ("healthcare", "hospital") and M({"amenity": "school"}) == ("education", "school")
      and M({"amenity": "police"}) == ("emergency", "police"))
check("new service categories: toilets → sanitation, ATM → banking, community centre → welfare",
      M({"amenity": "toilets"})[0] == "sanitation" and M({"amenity": "atm"})[0] == "banking" and M({"amenity": "community_centre"})[0] == "welfare")
check("points of interest: park → recreation, mall → commerce, museum → religious & cultural",
      M({"leisure": "park"})[0] == "recreation" and M({"shop": "mall"})[0] == "commerce" and M({"tourism": "museum"})[0] == "religious")
check("every service category is a known category", f.SERVICE_CATEGORIES <= {c for c, _ in f.POI_FILTERS.values()}, f.SERVICE_CATEGORIES)
check("an ordinary shop is not a facility", M({"shop": "clothes"}) is None)
print("ALL PASS" if not fails else "FAILED: %d" % len(fails)); sys.exit(1 if fails else 0)
