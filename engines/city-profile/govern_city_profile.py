#!/usr/bin/env python3
"""Govern city.cell_profile (the City Profile rollup's output). Idempotent; the schema-request → decide flow, as
platform-spec/deploy/ec2/govern_metering.py does for metering.usage.

Run INSIDE the target network container (it reaches the Network + identity on the compose network):
    docker exec -e NETWORK_URL=http://network:8090 -e IDENTITY_URL=http://identity:8093 <network> python3 - < govern_city_profile.py
Modes: apply (default) | snapshot (read-only: is it governed?). Env: NETWORK_URL, IDENTITY_URL, DEV_USER, STEWARD_USER.
"""
import json, os, sys, urllib.error, urllib.request

NETWORK = os.environ.get("NETWORK_URL", "http://network:8090")
IDENTITY = os.environ.get("IDENTITY_URL", "http://identity:8093")
DEV_USER = os.environ.get("DEV_USER", "devrick")
STEWARD_USER = os.environ.get("STEWARD_USER", "steward")
SID, VER = "city.cell_profile", "0.1.0"
IDP = r"^ds:[a-z0-9-]+:\S+$"


def call(url, body=None, token=None):
    hdr = {"Content-Type": "application/json"}
    if token:
        hdr["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr), timeout=40).read() or b"{}")


def login(u):
    return call(IDENTITY + "/login", {"username": u})["access_token"]


def defn():
    S, N, NUM = {"type": "string"}, {"type": ["number", "null"]}, {"type": ["number", "null"]}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://airawat.org/domain/city/0001/city.cell_profile.schema.json",
            "title": "city.cell_profile", "type": "object", "x-kind": "measure", "x-subject": "cell", "x-layer": "L3",
            "description": "One H3 cell of a city, joined across its baseline layers (heat, rainfall, forecast, built-up "
                           "and green cover, hydrology, population, buildings, roads, facilities). A DERIVED, rebuildable "
                           "rollup written by engine/city-profile — the source layers remain the record of truth. A "
                           "null field means that layer has not been built for this cell, never zero; `layers` maps each "
                           "source schema to its observation time.",
            "additionalProperties": False,
            "required": ["schema", "id", "tenant", "cell_ref", "layers", "layers_present", "observed_at", "prov"],   # house rule R5: a measure requires prov
            "properties": {
                "schema": {"const": SID}, "id": {"type": "string", "pattern": IDP}, "tenant": S, "aoi": S,
                "cell_ref": {"type": "object", "required": ["h3_id"],
                             "properties": {"h3_id": S, "centroid": {"type": "array"}}},
                "heat_c": N, "rainfall_mm": N, "forecast_peak_precip_mm": N, "forecast_total_precip_mm": N,
                "builtup_frac": N, "green_frac": N,
                "flood_susceptibility": NUM, "hand_mean_m": N, "drainage_present": {"type": ["boolean", "null"]},
                "population": N, "density_per_km2": N,
                "building_coverage": N, "mean_height_m": N, "built_volume_m3": N,
                "road_density_km_per_km2": N, "road_length_m": N,
                "facility_count": {"type": ["integer", "null"]},
                "facilities": {"type": "object", "additionalProperties": {"type": "integer"}},
                "layers": {"type": "object", "additionalProperties": {"type": ["string", "null"]}},
                "layers_present": {"type": "array", "items": S},
                "observed_at": S, "prov": {"type": "object"}}}


def main():
    if (sys.argv[1] if len(sys.argv) > 1 else "apply") == "snapshot":
        d = call(NETWORK + "/v1/schemas")
        items = d if isinstance(d, list) else d.get("schemas") or d.get("items") or []
        print("%s governed:" % SID, any((r.get("schema_id") or "").split("@")[0].endswith(SID) for r in items))
        return 0
    dev, steward = login(DEV_USER), login(STEWARD_USER)
    try:
        call(NETWORK + "/v1/domains", {"domain": "city", "label": "City digital baseline (layers, rollups, coverage)"}, steward)
    except urllib.error.HTTPError as e:
        if e.code not in (400, 409):
            raise
    try:
        call(NETWORK + "/v1/schema-requests", {"schema_id": SID, "version": VER, "definition": defn(), "sensitivity": "internal",
             "justification": "City Profile rollup: one joined record per cell for City Scan Ask and apps."}, dev)
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raw = e.read() or b"{}"
            try:
                d = json.loads(raw).get("detail", raw.decode(errors="replace"))
            except Exception:
                d = raw.decode(errors="replace")
            raise SystemExit("FAIL (%s): %s" % (e.code, json.dumps(d) if not isinstance(d, str) else d))
    call(NETWORK + "/v1/schema-requests/decide", {"schema_id": SID, "version": VER, "decision": "approve"}, steward)
    print("ok %s@%s governed" % (SID, VER))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
