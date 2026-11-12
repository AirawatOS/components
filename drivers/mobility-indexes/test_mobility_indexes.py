#!/usr/bin/env python3
"""Behavioural check — Mobility Indexes on a small synthetic network (no osmium / h3 needed).

  • a street GRID cell (many 4-way junctions, no dead ends) scores higher walkability than a CUL-DE-SAC cell;
  • motorways do not count as walkable streets;
  • transit gap: a populous cell far from any bus stop scores high; a cell next to a stop scores 0; a metro station
    within 1 km halves the gap; no stops at all → no records;
  • chokepoints: the single arterial BRIDGE between two clusters carries the most shortest routes (score 100);
  • degree-2 chains are contracted (the graph stays small).
Exit 1 on failure.
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import run as mi   # noqa: E402

fails = []
def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + str(detail)[:200] if detail else ""))
    if not ok: fails.append(name)

D = 0.001                                   # ~110 m
def cell_of(x, y):                          # two "cells": west (x < 77.05) and east
    return "W" if x < 77.05 else "E"
area = lambda h: 1.0

# west cell: a 4×4 street grid; east cell: a branching cul-de-sac tree
ways, nid = [], 0
def node(x, y):
    return (round(x * 1e6) * 10 ** 7 + round(y * 1e6), x, y)
for i in range(4):
    ways.append(("residential", [node(77.0 + i * D, 12.0 + j * D) for j in range(4)]))
    ways.append(("residential", [node(77.0 + j * D, 12.0 + i * D) for j in range(4)]))
trunk = [node(77.06, 12.0), node(77.06 + D, 12.0), node(77.06 + 2 * D, 12.0)]
ways.append(("residential", trunk))
for k in range(3):
    ways.append(("residential", [trunk[k], node(77.06 + k * D, 12.0 + D)]))
ways.append(("motorway", [node(77.0, 12.01), node(77.004, 12.01)]))     # a motorway in W — not walkable
per = mi.street_measures(ways, cell_of)
wk = mi.walkability(per, area)
check("grid cell (W) out-scores the cul-de-sac cell (E)", wk["W"]["walkability_score"] > wk["E"]["walkability_score"],
      (wk["W"], wk["E"]))
check("…the grid has 4-way junctions and no dead ends; the tree has dead ends",
      wk["W"]["intersection_density_per_km2"] >= 4 and wk["E"]["dead_end_share"] > 0.5, (wk["W"], wk["E"]))
check("motorways are not counted as walkable street length",
      round(per["W"]["street_m"]) == round(sum(mi.hav(a[1], a[2], b[1], b[2]) for c, n in ways if c == "residential" and n[0][1] < 77.05 for a, b in zip(n, n[1:]))))

cells = {"near": (77.0, 12.0, 5000), "far": (77.03, 12.0, 8000), "far_metro": (77.07, 12.0, 8000), "empty": (77.05, 12.0, 0)}
bus = [(77.0005, 12.0)]
metro = [(77.072, 12.003)]                       # within 1 km of far_metro only
ta = mi.transit_access(cells, bus, metro)
check("a cell next to a bus stop has no gap (score 0, not underserved)", ta["near"]["transit_gap_score"] == 0 and not ta["near"]["underserved"], ta["near"])
check("a populous cell ~3 km from any stop is underserved with the top score", ta["far"]["underserved"] and ta["far"]["transit_gap_score"] == 100.0, ta["far"])
check("…a metro within 1 km halves the gap (far_metro scores below far)", ta["far_metro"]["transit_gap_score"] < ta["far"]["transit_gap_score"], (ta["far_metro"], ta["far"]))
check("distances are reported in metres", 2000 < ta["far"]["dist_bus_stop_m"] < 4000 and ta["far_metro"]["dist_metro_m"] < 1000 and ta["far"]["dist_metro_m"] > 1000, ta["far"])
check("no stops at all → no transit records", mi.transit_access(cells, [], []) == {})

# two arterial clusters joined by ONE bridge link
A = [("primary", [node(77.10 + i * D, 12.1), node(77.10 + i * D, 12.1 + D), node(77.10 + i * D, 12.1 + 2 * D)]) for i in range(3)]
A += [("primary", [node(77.10, 12.1 + j * D), node(77.10 + D, 12.1 + j * D), node(77.10 + 2 * D, 12.1 + j * D)]) for j in range(3)]
B = [("primary", [node(77.12 + i * D, 12.1), node(77.12 + i * D, 12.1 + D), node(77.12 + i * D, 12.1 + 2 * D)]) for i in range(3)]
B += [("primary", [node(77.12, 12.1 + j * D), node(77.12 + D, 12.1 + j * D), node(77.12 + 2 * D, 12.1 + j * D)]) for j in range(3)]
bridge = [("secondary", [node(77.10 + 2 * D, 12.1 + D), node(77.11, 12.1 + D), node(77.11 + D, 12.1 + D), node(77.12, 12.1 + D)])]
cell2 = lambda x, y: "BRIDGE" if 77.103 < x < 77.119 else ("A%d" % round((x - 77.10) / D) if x < 77.11 else "B%d" % round((x - 77.12) / D))
adj, edges = mi.arterial_graph(A + B + bridge)
ch = mi.chokepoints(adj, edges, cell2, 1000)
top = max(ch, key=lambda h: ch[h]["betweenness"])
check("the single bridge between two clusters is the top chokepoint", top == "BRIDGE" and ch["BRIDGE"]["chokepoint_score"] == 100.0, (top, ch.get("BRIDGE")))
check("degree-2 chains are contracted (bridge = one edge, not three)", sum(1 for e, m in edges.items() if len(m) == 3) >= 1, len(edges))
check("residential streets are not part of the arterial graph", mi.arterial_graph([w for w in ways if w[0] == "residential"])[0] == {})
print("ALL PASS" if not fails else "FAILED: %d" % len(fails)); sys.exit(1 if fails else 0)
