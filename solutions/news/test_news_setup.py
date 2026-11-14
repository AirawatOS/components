#!/usr/bin/env python3
"""News setup searches for the CITY, not the tenant id: on staging the tenant `janaagraha` (an organisation handle)
would have searched "Janaagraha civic" — news about the organisation. The AOI name ("…/space/aoi/bengaluru") names the
city. Drives the real main() with stubbed broker calls. Exit 1 on failure."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
os.environ.update({"TENANT": "janaagraha", "RUN_TOKEN": "t"})
import run as news   # noqa: E402
fails = []
def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + str(detail)[:200] if detail else ""))
    if not ok: fails.append(name)
written = {}
news.bquery = lambda scope: [{"id": "ds:janaagraha:space/aoi/bengaluru", "bbox": [77.4, 12.8, 77.8, 13.1]}] if scope == "space.aoi" else []
news.bwrite = lambda oid, obj: written.__setitem__(oid, obj)
news.main()
q = [o["env"]["QUERIES"] for o in written.values()]
check("two schedules written (RSS + GDELT)", len(written) == 2, sorted(written))
check("the queries name the city from the AOI (Bengaluru), not the tenant id",
      all("Bengaluru civic@janaagraha" in x and "Janaagraha" not in x for x in q), q[:1])
news.bquery = lambda scope: []
written.clear(); news.main()
check("no AOI yet → falls back to the tenant id (as before)", all("Janaagraha civic@janaagraha" in o["env"]["QUERIES"] for o in written.values()))
print("ALL PASS" if not fails else "FAILED: %d" % len(fails)); sys.exit(1 if fails else 0)
