#!/usr/bin/env python3
"""osm-water's pure logic: which tags are lake-like (rivers / canals / pools are not), the area of a ring with a hole,
and the size thresholds (named ≥ 0.5 ha, unnamed ≥ 2 ha). Exit 1 on failure."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TENANT", "t")
import run as ow

fails = []
def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + str(detail)[:200] if detail else ""))
    if not ok: fails.append(name)

K = ow.water_kind
check("lake / reservoir / pond / wetland / named-kere water are lake-like",
      [K({"natural": "water", "water": "lake"}), K({"landuse": "reservoir"}), K({"natural": "water", "water": "pond"}),
       K({"natural": "wetland"}), K({"natural": "water", "name": "Agara Kere"}), K({"natural": "water"})]
      == ["lake", "reservoir", "pond", "wetland", "lake", "water"])
check("rivers, canals, swimming pools and waterways are NOT",
      [K({"natural": "water", "water": "river"}), K({"natural": "water", "water": "canal"}), K({"leisure": "swimming_pool", "natural": "water"}),
       K({"waterway": "riverbank", "natural": "water"}), K({"building": "yes"})] == [None] * 5)
sq = [[77.0, 13.0], [77.01, 13.0], [77.01, 13.01], [77.0, 13.01], [77.0, 13.0]]       # ~1.08 km × 1.1 km
hole = [[77.004, 13.004], [77.006, 13.004], [77.006, 13.006], [77.004, 13.006], [77.004, 13.004]]
a, ah = ow.area_m2([sq]), ow.area_m2([sq, hole])
check("area of a ~1.1 km square ≈ 1.2 km², and a hole is subtracted", 1.1e6 < a < 1.3e6 and 0 < a - ah < 0.06e6, (round(a), round(ah)))
small = [[[77.0, 13.0], [77.0006, 13.0], [77.0006, 13.0006], [77.0, 13.0006], [77.0, 13.0]]]   # ~0.43 ha
el = lambda tags: {"otype": "way", "id": 1, "tags": tags, "kind": "pond", "polygons": [small]}
check("a ~0.4 ha UNNAMED pond is dropped (< 2 ha) and so is a named one (< 0.5 ha)",
      ow.to_feature(el({"natural": "water"})) is None and ow.to_feature(el({"natural": "water", "name": "X"})) is None)
f = ow.to_feature({"otype": "relation", "id": 42, "tags": {"natural": "water", "water": "lake", "name": "Bellandur Lake", "highway": "x"},
                   "kind": "lake", "polygons": [[sq, hole]]})
check("a lake relation → geo.osm_feature Polygon with name, kind, area in tags; unrelated tags dropped; relation id",
      f["geometry"]["type"] == "Polygon" and f["tags"]["name"] == "Bellandur Lake" and f["tags"]["_water_kind"] == "lake"
      and "highway" not in f["tags"] and f["id"].endswith("/relation/42") and f["source"]["via"] == ow.VIA, f["id"])
print("ALL PASS" if not fails else "FAILED: %d" % len(fails))
sys.exit(1 if fails else 0)
