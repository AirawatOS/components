#!/usr/bin/env python3
"""Behavioural check — water-hotspots aggregates complaints into H3 hotspots the City Scan map can draw.

Runs the real engine against the aos in-memory registry with a FIXED complaint set (30-day window, 3 cells):
  • one water.hotspot per cell with ≥1 in-window complaint; `count` and the modal `dominant` category are right;
  • every hotspot carries the flat `h3_id` (== `h3`) AND the governed `space` block with `space.h3` + a name for the
    map hover (commits 5a63316 / d9379d2 — what the City Scan layers read);
  • severity ranks by count: the top ceil(N*crit_pct) cells critical, up to ceil(N*warn_pct) warning, rest info;
  • complaints outside the window and non-complaint record kinds (a query) are excluded.
Exit 1 on failure.
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "_common"))
from aos import driver  # noqa: E402
import run as wh        # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + detail if detail else ""))
    if not ok:
        fails.append(name)


T = "testcity"
A, B, C = "8961892cd8bffff", "8961892cd93ffff", "8961892cdb7ffff"
seed = {}


def put(o):
    seed[o["id"]] = o


put({"schema": "water.record_kind", "id": "rk-c", "code": "complaint", "is_complaint": True})
put({"schema": "water.record_kind", "id": "rk-q", "code": "query", "is_complaint": False})
put({"schema": "water.config", "id": "cfg", "severity": {"count_crit_pct": 0.30, "count_warn_pct": 0.60}, "windows": {"min_count": 1}})
n = 0
for h, k, cat, day in ([(A, "complaint", "no_water", "2026-10-%02d" % d) for d in range(10, 16)]       # 6 in A (5 no_water, then 1 below)
                       + [(A, "complaint", "low_pressure", "2026-10-20")]
                       + [(B, "complaint", "low_pressure", "2026-10-%02d" % d) for d in (18, 19, 20)]
                       + [(C, "complaint", "contamination", "2026-10-21")]
                       + [(C, "query", "contamination", "2026-10-21")]                                  # a query: excluded
                       + [(B, "complaint", "low_pressure", "2026-08-01")]):                             # outside the window
    n += 1
    put({"schema": "water.complaint", "id": "c%d" % n, "h3": h, "record_kind": k, "category": cat,
         "registered_at": day + "T09:00:00Z", "ward": "W%s" % h[-6], "zone": "Z1"})

store = driver.run_local(seed, tenant=T)
hs = {o["h3"]: o for o in store.values() if o.get("schema") == "water.hotspot"}
check("one hotspot per cell with in-window complaints (3 cells)", sorted(hs) == sorted([A, B, C]), str(sorted(hs)))
check("counts: A=7, B=3, C=1 (the query and the out-of-window complaint excluded)",
      (hs.get(A, {}).get("count"), hs.get(B, {}).get("count"), hs.get(C, {}).get("count")) == (7, 3, 1),
      str({h[-7:]: o.get("count") for h, o in hs.items()}))
check("dominant = the modal category", hs.get(A, {}).get("dominant") == "no_water" and hs.get(B, {}).get("dominant") == "low_pressure")
check("every hotspot carries h3_id == h3 (flat, back-compat)", all(o.get("h3_id") == h for h, o in hs.items()))
check("every hotspot carries the governed space block (space.h3 + a hover name)",
      all((o.get("space") or {}).get("h3") == h and (o.get("space") or {}).get("name") for h, o in hs.items()),
      str({h[-7:]: o.get("space") for h, o in list(hs.items())[:1]}))
check("severity by count percentile: A critical, B warning, C info",
      (hs.get(A, {}).get("severity"), hs.get(B, {}).get("severity"), hs.get(C, {}).get("severity")) == ("critical", "warning", "info"),
      str({h[-7:]: o.get("severity") for h, o in hs.items()}))
check("the window ends on the latest complaint and spans 30 days",
      all((o.get("window") or {}).get("to") == "2026-10-21" and (o.get("window") or {}).get("days") == 30 for o in hs.values()))

print("ALL PASS" if not fails else "FAILED: %d" % len(fails))
sys.exit(1 if fails else 0)
