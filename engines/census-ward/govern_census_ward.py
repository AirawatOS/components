#!/usr/bin/env python3
"""Govern demographics.census_ward (Census Ward's output) — filed and approved by the standards-body steward. Idempotent.
    docker exec -i <network> python3 - < govern_census_ward.py
"""
import json, os, urllib.error, urllib.request
NETWORK = os.environ.get("NETWORK_URL", "http://network:8090")
IDENTITY = os.environ.get("IDENTITY_URL", "http://identity:8093")
SID, VER = "demographics.census_ward", "0.1.0"
def call(url, body=None, token=None):
    hdr = {"Content-Type": "application/json"}
    if token: hdr["Authorization"] = "Bearer " + token
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=hdr), timeout=40).read() or b"{}")
def defn():
    S, N = {"type": "string"}, {"type": ["number", "null"]}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://airawat.org/domain/demographics/0001/%s.schema.json" % SID, "title": SID, "type": "object",
            "x-kind": "measure", "x-subject": "census_ward", "x-layer": "L3", "additionalProperties": False,
            "description": "One H3 cell carrying its WARD's official census figures (population, area, density) — the official "
                           "cross-check beside the modelled GHSL grid. Derived, rebuildable (engine/census-ward).",
            "required": ["schema", "id", "tenant", "cell_ref", "ward_no", "census_year", "observed_at", "prov"],
            "properties": {"schema": {"const": SID}, "id": {"type": "string", "pattern": r"^ds:[a-z0-9-]+:\S+$"}, "tenant": S,
                           "cell_ref": {"type": "object", "required": ["h3_id"], "properties": {"h3_id": S, "centroid": {"type": "array"}}},
                           "ward_no": S, "ward_name": {"type": ["string", "null"]}, "census_year": S, "census_population": N,
                           "area_km2": N, "census_density_per_km2": N, "observed_at": S, "prov": {"type": "object"}}}
def main():
    tok = call(IDENTITY + "/login", {"username": os.environ.get("STEWARD_USER", "steward")})["access_token"]
    try:
        call(NETWORK + "/v1/schema-requests", {"schema_id": SID, "version": VER, "definition": defn(), "sensitivity": "public",
             "justification": "Census Ward: the official 2011 census by ward on the city grid (cross-check for GHSL)."}, tok)
    except urllib.error.HTTPError as e:
        if e.code != 409: raise SystemExit("FAIL (%s): %s" % (e.code, (e.read() or b"")[:300].decode(errors="replace")))
    call(NETWORK + "/v1/schema-requests/decide", {"schema_id": SID, "version": VER, "decision": "approve"}, tok)
    print("ok %s@%s governed" % (SID, VER)); return 0
if __name__ == "__main__":
    raise SystemExit(main())
