#!/usr/bin/env python3
"""Every driver that imports osmium declares the geospatial runtime (the only runtime that ships osmium). On staging
osm-facilities ran as a plain host process and failed with "No module named 'osmium'". Exit 1 on failure."""
import os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
bad = []
for d in sorted(os.listdir(HERE)):
    run, meta = os.path.join(HERE, d, "run.py"), os.path.join(HERE, d, "meta.yaml")
    if not (os.path.isfile(run) and os.path.isfile(meta)):
        continue
    if re.search(r"^\s*import osmium|^\s*from osmium", open(run).read(), re.M):
        m = open(meta).read()
        ok = re.search(r"^runtime:\s*\n\s+profile:\s*geospatial\b", m, re.M)
        print(("  PASS " if ok else "  FAIL ") + d + " imports osmium → runtime profile geospatial")
        if not ok:
            bad.append(d)
print("ALL PASS" if not bad else "FAILED: %d" % len(bad))
sys.exit(1 if bad else 0)
