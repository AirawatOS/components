#!/usr/bin/env python3
"""Ward Air Quality Alert — a composed SOLUTION bundle (INT-0003). Installing it fans out to its members."""
MEMBERS = ["ds:airawat:driver/air-sensors", "ds:airawat:app/ward-air-board"]

def main():
    print("Solution:", "Ward Air Quality Alert")
    for m in MEMBERS: print("  -", m)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
