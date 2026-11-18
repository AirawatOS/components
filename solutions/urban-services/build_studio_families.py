#!/usr/bin/env python3
"""Embed families.json (from families.py) into Service Studio as its 'start from a family' catalogue."""
import json, os, re
here = os.path.dirname(os.path.abspath(__file__))
fam = json.load(open(os.path.join(here, "families.json")))
p = os.path.join(here, "..", "..", "apps", "service-studio", "dist", "index.html")
s = open(p).read()
block = "/*FAMILIES*/const FAMILIES=" + json.dumps(fam, ensure_ascii=False, separators=(",", ":")) + ";/*/FAMILIES*/"
s = re.sub(r"/\*FAMILIES\*/.*?/\*/FAMILIES\*/", lambda m: block, s, flags=re.S) if "/*FAMILIES*/" in s else s.replace("// ================= the EDITOR", block + "\n// ================= the EDITOR", 1)
open(p, "w").write(s); print("embedded", len(fam), "families,", len(block), "bytes")
