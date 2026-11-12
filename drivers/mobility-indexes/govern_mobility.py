#!/usr/bin/env python3
"""Govern the Mobility Indexes outputs (mobility.walkability, mobility.transit_access, mobility.chokepoint) and the
`mobility` domain — schema-request → decide, filed and approved by the standards-body steward. Idempotent.
    docker exec -i <network> python3 - < govern_mobility.py
"""
import json, os, urllib.error, urllib.request

NETWORK = os.environ.get("NETWORK_URL", "http://network:8090")
IDENTITY = os.environ.get("IDENTITY_URL", "http://identity:8093")
STEWARD_USER = os.environ.get("STEWARD_USER", "steward")
VER = "0.1.0"
IDP = r"^ds:[a-z0-9-]+:\S+$"


def call(url, body=None, token=None):
    hdr = {"Content-Type": "application/json"}
    if token:
        hdr["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr), timeout=40).read() or b"{}")


def defn(sid, subject, desc, fields):
    S = {"type": "string"}
    props = {"schema": {"const": sid}, "id": {"type": "string", "pattern": IDP}, "tenant": S,
             "cell_ref": {"type": "object", "required": ["h3_id"], "properties": {"h3_id": S, "centroid": {"type": "array"}}},
             "observed_at": S, "prov": {"type": "object"}}
    props.update(fields)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://airawat.org/domain/mobility/0001/%s.schema.json" % sid,
            "title": sid, "type": "object", "x-kind": "measure", "x-subject": subject, "x-layer": "L3",
            "description": desc, "additionalProperties": False,
            "required": ["schema", "id", "tenant", "cell_ref", "observed_at", "prov"], "properties": props}


N, I, B = {"type": ["number", "null"]}, {"type": ["integer", "null"]}, {"type": ["boolean", "null"]}
SCHEMAS = [
    ("mobility.walkability", "street_network",
     "Walkability of one H3 cell from OSM street topology: junction density, street density and dead-end share, each a "
     "percentile within the city, averaged (walkability_score 0–100). Derived, rebuildable (driver/mobility-indexes).",
     {"walkability_score": N, "intersection_density_per_km2": N, "street_density_km_per_km2": N, "dead_end_share": N}),
    ("mobility.transit_access", "transit_access",
     "Public-transport access of one H3 cell: straight-line distance to the nearest OSM bus stop and metro station, the "
     "cell's population, and transit_gap_score (0–100) — people beyond a walkable stop, as a city percentile.",
     {"transit_gap_score": N, "dist_bus_stop_m": N, "dist_metro_m": N, "population": N, "underserved": B}),
    ("mobility.chokepoint", "road_segment",
     "How many shortest travel-time routes on the OSM arterial network pass through one H3 cell (sampled edge "
     "betweenness; chokepoint_score 0–100 city percentile). A PROXY for where congestion concentrates — not observed traffic.",
     {"chokepoint_score": N, "betweenness": N}),
]


def main():
    tok = call(IDENTITY + "/login", {"username": STEWARD_USER})["access_token"]
    try:
        call(NETWORK + "/v1/domains", {"domain": "mobility", "label": "Mobility (streets, transit access, network)"}, tok)
    except urllib.error.HTTPError as e:
        if e.code not in (400, 409):
            raise
    for sid, subject, desc, fields in SCHEMAS:
        try:
            call(NETWORK + "/v1/schema-requests", {"schema_id": sid, "version": VER, "definition": defn(sid, subject, desc, fields),
                 "sensitivity": "internal", "justification": "Mobility Indexes: walkability, transit access gap, arterial chokepoints."}, tok)
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise SystemExit("FAIL (%s) %s: %s" % (e.code, sid, (e.read() or b"")[:300].decode(errors="replace")))
            print("  %s: %s" % (sid, (e.read() or b"")[:200].decode(errors="replace")))
        try:
            call(NETWORK + "/v1/schema-requests/decide", {"schema_id": sid, "version": VER, "decision": "approve"}, tok)
            print("ok %s@%s governed" % (sid, VER))
        except urllib.error.HTTPError as e:
            print("  decide %s: %s %s" % (sid, e.code, (e.read() or b"")[:200].decode(errors="replace")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
