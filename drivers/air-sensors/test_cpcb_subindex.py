#!/usr/bin/env python3
"""CPCB's data.gov.in "Real time Air Quality Index" dataset reports each pollutant's AQI SUB-INDEX, not µg/m³.
On staging, Jayanagar 5th Block's PM2.5 sub-index 213 (CPCB dashboard: AQI 205; ~94 µg/m³) was written as 213 µg/m³ and the whole
city became one "critical" hotspot. Drives the real ingest_cpcb() with data.gov.in-shaped records and checks the
written PM2.5 / NO2 / O3 are concentrations (NAQI breakpoints inverted) and the method says so. Exit 1 on failure."""
import os, sys, types
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
if "aos" not in sys.modules:                                   # the confined SDK is not needed to test the parser
    aos = types.ModuleType("aos"); aos.driver = types.SimpleNamespace(main=lambda f: f); aos.ctx = None; sys.modules["aos"] = aos
import run as ar   # noqa: E402

fails = []
def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + str(detail)[:200] if detail else ""))
    if not ok: fails.append(name)

check("PM2.5 sub-index 213 → 93.9 µg/m³ (Poor band 200–300 ↔ 90–120)", ar.subindex_to_conc("pm25", 213) == 93.9, ar.subindex_to_conc("pm25", 213))
check("PM2.5 band edges: 50→30, 100→60, 200→90, 300→120", [ar.subindex_to_conc("pm25", i) for i in (50, 100, 200, 300)] == [30, 60, 90, 120])
check("NO2 / O3 use their own tables (NO2 100→80, O3 200→168)", (ar.subindex_to_conc("no2", 100), ar.subindex_to_conc("o3", 200)) == (80, 168))
check("unknown pollutant / missing / negative → None (never guessed)",
      (ar.subindex_to_conc("so2", 50), ar.subindex_to_conc("pm25", None), ar.subindex_to_conc("pm25", -1)) == (None, None, None))

REC = lambda st, lat, lon, pol, v: {"station": st, "latitude": str(lat), "longitude": str(lon), "pollutant_id": pol,
                                    "avg_value": str(v), "last_update": "11-11-2026 23:00:00"}
PAGE = {"records": [REC("Jayanagar 5th Block, Bengaluru - KSPCB", 12.92, 77.58, "PM2.5", 213),
                    REC("Jayanagar 5th Block, Bengaluru - KSPCB", 12.92, 77.58, "NO2", 40),
                    REC("BTM Layout, Bengaluru - CPCB", 12.91, 77.61, "PM2.5", 77),
                    REC("Anand Vihar, Delhi - DPCC", 28.65, 77.31, "PM2.5", 400)]}      # outside the AOI
ar._fetch = lambda url, inject=None: (200, PAGE if "offset=0" in url else {"records": []})
ar.h3 = types.SimpleNamespace(latlng_to_cell=lambda lat, lng, res: "h3-%s-%s" % (lat, lng))
out = []
n = ar.ingest_cpcb("janaagraha", [77.46, 12.83, 77.78, 13.14], 9, out)
by = {o["sensor_id"]: o for o in out}
jay = next((o for k, o in by.items() if "jayanagar" in k), {})
btm = next((o for k, o in by.items() if "btm" in k), {})
check("two in-AOI stations written (Delhi row ignored)", n == 2 and len(out) == 2, sorted(by))
check("Jayanagar PM2.5 written as 93.9 µg/m³, not 213", jay.get("pm25") == 93.9, jay.get("pm25"))
check("…its NO2 sub-index 40 → 32 µg/m³", jay.get("no2") == 32.0, jay.get("no2"))
check("BTM PM2.5 sub-index 77 → 46.2 µg/m³ (Satisfactory — not a hotspot)", btm.get("pm25") == 46.2, btm.get("pm25"))
check("the method records the conversion", "sub-index" in (jay.get("prov") or {}).get("method", ""), (jay.get("prov") or {}).get("method"))
print("ALL PASS" if not fails else "FAILED: %d" % len(fails)); sys.exit(1 if fails else 0)
