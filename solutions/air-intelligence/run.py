#!/usr/bin/env python3
"""Air Intelligence — a composed SOLUTION bundle (INT-0003). Installing it fans out to its members."""
MEMBERS = ["ds:airawat:driver/air-sensors", "ds:airawatdeveloper:driver/air-lowcost-sensors", "ds:airawat:engine/station-interpolation", "ds:airawat:engine/hotspot-detection", "ds:airawat:skill/air-quality-advisory", "ds:airawat:app/air-explorer", "ds:airawat:engine/decision-router", "ds:airawat:app/inbox"]

def main():
    print("Solution:", "Air Intelligence")
    for m in MEMBERS: print("  -", m)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
