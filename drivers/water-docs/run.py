#!/usr/bin/env python3
"""water-docs — seed the Water Intelligence Build + Install guides into the tenant's Docs (doc.document).
Idempotent: deterministic ids, re-run safe. Ships the guide markdown alongside this run.py (./guides/*.md),
so it is fully self-contained and confined (only I/O is the SDK ctx)."""
import os
from aos import driver, ctx

GUIDES = [("water-intelligence-build-guide", "Water Intelligence — Build Guide (Console)", "build-guide.md"),
          ("water-intelligence-install-guide", "Water Intelligence — Install Guide", "install-guide.md")]


@driver.main
def run():
    T = ctx.tenant
    if not T:
        print("FAIL: no run tenant."); return 1
    here = os.path.dirname(os.path.abspath(__file__))
    rows = []
    for slug, title, fn in GUIDES:
        p = os.path.join(here, "guides", fn)
        if not os.path.exists(p):
            print("[water-docs] missing guide %s — skipped." % fn); continue
        body = open(p, encoding="utf-8").read()
        oid = "ds:%s:doc.document/%s" % (T, slug)
        rows.append({"schema": "doc.document", "id": oid, "tenant": T, "owner": ctx.sub if hasattr(ctx, "sub") else None,
                     "title": title, "body": body, "format": "markdown", "folder": "Water Intelligence"})
    rows = [{k: v for k, v in r.items() if v is not None} for r in rows]
    wrote = ctx.write_many(rows) or 0 if rows else 0
    print("[water-docs] seeded %d guide document(s) into Docs (folder 'Water Intelligence')." % wrote)
    return 0


if __name__ == "__main__":
    raise SystemExit(driver.run())
