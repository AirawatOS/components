#!/usr/bin/env python3
"""City Digital Baseline — a composed SOLUTION bundle (INT-0003 / ADR-0035). Installing it fans out to its
members: the City Setup wizard, the City Scan viewer, and every baseline ingest driver + cell engine."""
MEMBERS = [
    "ds:airawat:app/city-setup", "ds:airawat:app/city-scan",
    "ds:airawat:driver/landcover-dw", "ds:airawat:driver/heat", "ds:airawat:driver/terrain-dem",
    "ds:airawat:driver/rainfall-openmeteo", "ds:airawat:driver/weather-forecast", "ds:airawat:driver/hydro-merit",
    "ds:airawat:driver/osm-roads", "ds:airawat:driver/osm-facilities",
    "ds:airawat:driver/buildings-openbuildings", "ds:airawat:driver/buildings-footprints",
    "ds:airawat:driver/population-ghsl",
    "ds:airawat:engine/cell-indexer", "ds:airawat:engine/coverage-qa",
]


def main():
    print("Solution:", "City Digital Baseline")
    for m in MEMBERS:
        print("  -", m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
