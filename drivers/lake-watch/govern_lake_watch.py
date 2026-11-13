#!/usr/bin/env python3
"""Govern the Lake Watch and night-lights-trend outputs — water.lake, water.lake_cell, econ.nightlight_trend —
schema-request → decide, filed and approved by the standards-body steward. NEW schemas (not version bumps of an
existing one), so the kernel's per-id validator cache needs no restart. Idempotent.
    docker exec -i <app-host> python3 - < govern_lake_watch.py
"""
import json, os, urllib.error, urllib.request

NETWORK = os.environ.get("NETWORK_URL", "http://network:8090")
IDENTITY = os.environ.get("IDENTITY_URL", "http://identity:8093")
STEWARD_USER = os.environ.get("STEWARD_USER", "steward")
VER = "0.1.0"
IDP = r"^ds:[a-z0-9-]+:\S+$"
S, N, I = {"type": "string"}, {"type": ["number", "null"]}, {"type": ["integer", "null"]}
SN = {"type": ["string", "null"]}


def call(url, body=None, token=None):
    hdr = {"Content-Type": "application/json"}
    if token:
        hdr["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr), timeout=40).read() or b"{}")


def defn(sid, domain, subject, desc, fields, required=()):
    props = {"schema": {"const": sid}, "id": {"type": "string", "pattern": IDP}, "tenant": S,
             "cell_ref": {"type": "object", "required": ["h3_id"], "properties": {"h3_id": S, "centroid": {"type": "array"}}},
             "observed_at": S, "prov": {"type": "object"}}
    props.update(fields)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://airawat.org/domain/%s/0001/%s.schema.json" % (domain, sid),
            "title": sid, "type": "object", "x-kind": "measure", "x-subject": subject, "x-layer": "L3",
            "description": desc, "additionalProperties": False,
            "required": ["schema", "id", "tenant", "cell_ref", "observed_at", "prov"] + list(required), "properties": props}


YEAR_SERIES = lambda item: {"type": "array", "items": {"type": "object", "required": ["t"], "properties": dict({"t": S}, **item),
                                                       "additionalProperties": False}}
SCHEMAS = [
    ("water.lake", "water", "lake",
     "One lake / reservoir / named water body: its OSM polygon and name, its water area per year from satellites (JRC "
     "Global Surface Water 1985–2021, Dynamic World 2022+; a year with too much missing data is empty), and the built-up "
     "share of a ring around its shore (GHSL, 1975–2020), with a baseline-vs-recent summary. Derived (driver/lake-watch).",
     {"name": SN, "kind": SN, "osm_ref": SN, "geometry": {"type": "object"},
      "series": YEAR_SERIES({"water_area_m2": N, "nodata_share": N, "source": S}),
      "shore_ring_m": N, "shore_built": YEAR_SERIES({"built_share": N}),
      "baseline_water_m2": N, "recent_water_m2": N, "water_change_pct": N, "recent_years": {"type": "array"},
      "shore_built_1975": N, "shore_built_2020": N, "shore_built_change_pts": N, "osm_area_m2": N}, ("series",)),
    ("water.lake_cell", "water", "lake_cell",
     "The share of one H3 cell covered by water, per year (JRC Global Surface Water 1985–2021, Dynamic World 2022+), for "
     "the cells a lake covers — the layer that animates a lake's extent over time. Derived (driver/lake-watch).",
     {"lake": SN, "lake_ref": SN, "water_pct": N, "series": YEAR_SERIES({"water_pct": N})}, ("series",)),
    ("econ.nightlight_trend", "econ", "nightlight_trend",
     "Night-time light radiance of one H3 cell per year (VIIRS DNB monthly composites, yearly median, nW/cm²/sr) — a "
     "satellite PROXY for economic / energy activity over time; change_pct = the last 3 years vs the first 3. "
     "Derived (driver/nightlight-viirs ≥ 0.2.0).",
     {"avg_radiance": N, "first_year": SN, "last_year": SN, "change_pct": N, "series": YEAR_SERIES({"avg_radiance": N})}, ("series",)),
]


def main():
    tok = call(IDENTITY + "/login", {"username": STEWARD_USER})["access_token"]
    for sid, domain, subject, desc, fields, req in SCHEMAS:
        try:
            call(NETWORK + "/v1/schema-requests", {"schema_id": sid, "version": VER, "definition": defn(sid, domain, subject, desc, fields, req),
                 "sensitivity": "internal", "justification": "Lake Watch (lake extent + shore built-up over time) and the night-lights trend."}, tok)
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
