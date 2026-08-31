#!/usr/bin/env python3
"""Tax Gap Finder — a composed SOLUTION bundle (INT-0003). Installing it fans out to its members."""
MEMBERS = ["ds:airawatdeveloper:engine/tax-gap-engine", "ds:airawatdeveloper:app/tax-config-console", "ds:airawat:driver/geocode-ledger", "ds:airawat:driver/ingest-utilities", "ds:airawat:driver/change-detect-alphaearth", "ds:airawatdeveloper:engine/campaign-engine", "ds:airawatdeveloper:app/tax-gap-campaign"]

def main():
    print("Solution:", "Tax Gap Finder")
    for m in MEMBERS: print("  -", m)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
