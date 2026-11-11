#!/usr/bin/env python3
"""Behavioural check — water-setup seeds the Water Intelligence reference data AND the City Scan map layers.

Runs the real driver against the aos in-memory registry (the `aos test` path — same ctx surface as live):
  • a fresh tenant gets the full taxonomy (water.category, water.record_kind), one water.config, and the THREE
    City Scan viz.layer descriptors (water-load / water-critical / water-burden), each rendering water.hotspot on
    the map with its value field and colour scale (commit 5a63316);
  • a SECOND run is idempotent — nothing duplicated, same ids;
  • the critical layer filters severity=critical; the burden layer reads rate_per_10k.
Exit 1 on failure.
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "_common"))
from aos import driver  # noqa: E402
import run as ws        # noqa: E402  (registers the driver's @driver.main)

fails = []


def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + detail if detail else ""))
    if not ok:
        fails.append(name)


T = "testcity"
store = driver.run_local({}, tenant=T)
by = {}
for oid, o in store.items():
    by.setdefault(o.get("schema"), []).append(o)
check("taxonomy seeded: every water.category and water.record_kind",
      len(by.get("water.category", [])) == len(ws.CATEGORIES) and len(by.get("water.record_kind", [])) == len(ws.RECORD_KINDS),
      "%d categories, %d record kinds" % (len(by.get("water.category", [])), len(by.get("water.record_kind", []))))
check("one water.config", len(by.get("water.config", [])) == 1)
viz = {o["layer_id"]: o for o in by.get("viz.layer", [])}
check("the three City Scan water layers are written", sorted(viz) == ["water-burden", "water-critical", "water-load"], str(sorted(viz)))
check("every layer renders water.hotspot on the map, grouped under Water",
      all(o.get("source_schema") == "water.hotspot" and o.get("render") == "map" and o.get("group") == "Water" and o.get("enabled")
          for o in viz.values()))
check("load + critical layers read `count`, burden reads `rate_per_10k`",
      viz.get("water-load", {}).get("value_field") == "count" and viz.get("water-critical", {}).get("value_field") == "count"
      and viz.get("water-burden", {}).get("value_field") == "rate_per_10k")
check("the critical layer filters severity=critical", (viz.get("water-critical", {}).get("filters") or {}).get("severity") == "critical")
check("layer ids are tenant-scoped (ds:<tenant>:viz/layer/<id>)",
      all(o["id"] == "ds:%s:viz/layer/%s" % (T, k) for k, o in viz.items()))
again = driver.run_local(dict(store), tenant=T)
check("a second run is idempotent (no duplicates, same ids)", sorted(again) == sorted(store), "%d → %d objects" % (len(store), len(again)))

print("ALL PASS" if not fails else "FAILED: %d" % len(fails))
sys.exit(1 if fails else 0)
