#!/usr/bin/env python3
"""Org Foundation — Basic ERP — a composed SOLUTION bundle (INT-0003). Installing it fans out to its members."""
MEMBERS = ["ds:airawatdeveloper:app/org-setup", "ds:airawatdeveloper:app/hr-desk", "ds:airawatdeveloper:app/policy-console"]

def main():
    print("Solution:", "Org Foundation \u2014 Basic ERP")
    for m in MEMBERS: print("  -", m)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
