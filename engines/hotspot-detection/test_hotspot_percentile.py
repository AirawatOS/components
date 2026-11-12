#!/usr/bin/env python3
"""Hotspots are the city's own top 5% (0.3.0). On staging, a fixed 60 µg/m³ threshold turned broadly-elevated
Bengaluru (4 of 7 stations above 60) into ONE warning cluster of 8,336 of 11,118 cells. Drives the real run() on a
20×20 fake grid with stubbed ctx / h3 and checks: only the top 5% are selected; separate pockets stay separate;
severity is still the CPCB tier at the peak; a clean city (≤ 30 µg/m³) gets no hotspots. Exit 1 on failure."""
import os, sys, types
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)

fails = []
def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + str(detail)[:200] if detail else ""))
    if not ok: fails.append(name)

h3 = types.ModuleType("h3")
def grid_disk(c, k):
    x, y = map(int, c.split(","))
    return ["%d,%d" % (x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
h3.grid_disk = grid_disk; h3.cell_to_latlng = lambda c: (0.0, 0.0)
sys.modules["h3"] = h3

class Facts:
    def __init__(self, rows): self.rows = rows
    def list(self): return self.rows
class Ctx:
    tenant = "janaagraha"
    def __init__(self, grid): self.grid = grid; self.written = []
    def now(self): return "2026-11-12T02:30:00Z"
    def facts(self, schema):
        return Facts([{"space": {"h3": c, "res": 9, "centroid": [0, 0]}, "pm25": v, "observed_at": "2026-11-11T18:30:00Z"}
                      for c, v in self.grid.items()])
    def write_many(self, facts, **k): self.written += facts; return len(facts)

aos = types.ModuleType("aos"); aos.driver = types.SimpleNamespace(main=lambda f: f, run=lambda: 0); aos.ctx = None
sys.modules["aos"] = aos
import run as hd   # noqa: E402

def detect(grid):
    c = Ctx(grid); hd.ctx = c; hd.run(); return c.written

# a broadly elevated city: 62..70 everywhere, a Jayanagar-like pocket (88-92) and a smaller Silk-Board-like one (80)
# continuous background 60–70 (an interpolated surface has no ties); 21 pocket cells = the top 5% of 400 + 1
grid = {"%d,%d" % (x, y): 60 + ((x * 37 + y * 17) % 400) / 40 for x in range(20) for y in range(20)}
for i, c in enumerate(("2,2", "2,3", "3,2", "3,3", "2,4", "4,2", "3,4", "4,3", "4,4", "1,2", "2,1", "1,3", "1,1")):
    grid[c] = 88 + i % 4
grid["3,3"] = 91.5
for c in ("15,15", "15,16", "16,15", "16,16", "14,15", "15,14", "16,14", "14,16"):
    grid[c] = 80
hs = detect(grid)
cells = sum(h["cell_count"] for h in hs)
check("only the city's top 5% are hotspot cells (not every cell above 60)", 0 < cells <= 0.05 * len(grid) + 1, (cells, len(grid)))
check("two separate pockets stay two clusters", len(hs) == 2, [(h["space"]["h3"], h["cell_count"]) for h in hs])
peak = max(hs, key=lambda h: h["value"]) if hs else {}
check("severity is still the CPCB tier at the peak (91.5 → warning) and the cut is recorded",
      peak.get("severity") == "warning" and peak.get("value") == 91.5 and peak.get("threshold", 0) >= 80 and "p95" in peak.get("method", ""), peak)
check("a pocket below 90 at its peak is 'info'", any(h["severity"] == "info" and h["value"] == 80 for h in hs), [(h["value"], h["severity"]) for h in hs])

clean = {"%d,%d" % (x, y): 12 + (x + y) % 15 for x in range(20) for y in range(20)}   # all ≤ 26 µg/m³
check("a clean city (≤ 30 µg/m³ everywhere) gets no hotspots, however cells rank", detect(clean) == [])
print("ALL PASS" if not fails else "FAILED: %d" % len(fails)); sys.exit(1 if fails else 0)
