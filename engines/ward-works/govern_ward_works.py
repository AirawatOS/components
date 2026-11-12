#!/usr/bin/env python3
"""Govern works.ward_cell and procurement.ward_cell (Ward Works' outputs) — the schema-request → decide flow, as
engines/city-profile/govern_city_profile.py does. Idempotent.

Run INSIDE the target network container:
    docker exec -i -e NETWORK_URL=http://network:8090 -e IDENTITY_URL=http://identity:8093 <network> python3 - < govern_ward_works.py
Env: NETWORK_URL, IDENTITY_URL, DEV_USER, STEWARD_USER, DEV_REALM (optional; "airawatdeveloper" with DEVPW for a password realm).
"""
import json, os, urllib.error, urllib.request

NETWORK = os.environ.get("NETWORK_URL", "http://network:8090")
IDENTITY = os.environ.get("IDENTITY_URL", "http://identity:8093")
DEV_USER = os.environ.get("DEV_USER", "devrick")
DEV_REALM = os.environ.get("DEV_REALM", "")
STEWARD_USER = os.environ.get("STEWARD_USER", "steward")
VER = os.environ.get("SCHEMA_VERSION", "0.2.0")        # 0.2.0 adds works.ward_cell.series
IDP = r"^ds:[a-z0-9-]+:\S+$"


def call(url, body=None, token=None):
    hdr = {"Content-Type": "application/json"}
    if token:
        hdr["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr), timeout=40).read() or b"{}")


def login(user, realm=""):
    body = {"username": user}
    if realm and os.environ.get("DEVPW"):
        body["password"] = os.environ["DEVPW"]
    return call(IDENTITY + ("/realms/%s/login" % realm if realm else "/login"), body)["access_token"]


def defn(sid, domain, desc, values, subject):
    S, N = {"type": "string"}, {"type": ["number", "null"]}
    props = {"schema": {"const": sid}, "id": {"type": "string", "pattern": IDP}, "tenant": S,
             "cell_ref": {"type": "object", "required": ["h3_id"], "properties": {"h3_id": S, "centroid": {"type": "array"}}},
             "ward_no": S, "ward_name": {"type": ["string", "null"]}, "observed_at": S, "prov": {"type": "object"}}
    props.update({k: N for k in values})
    if sid == "works.ward_cell":                         # 0.2.0: yearly series (by work-order year) for the time slider
        props["series"] = {"type": "array", "items": {"type": "object", "required": ["t"], "additionalProperties": False,
                           "properties": {"t": S, **{k: N for k in values}}}}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://airawat.org/domain/%s/0001/%s.schema.json" % (domain, sid),
            "title": sid, "type": "object", "x-kind": "measure", "x-subject": subject, "x-layer": "L3",
            "description": desc, "additionalProperties": False,
            "required": ["schema", "id", "tenant", "cell_ref", "ward_no", "observed_at", "prov"], "properties": props}


SCHEMAS = [
    ("works.ward_cell", "works",
     "One H3 cell carrying its WARD's public-works figures (works count, net paid ₹) from works.ward_rollup — a derived, "
     "rebuildable map layer written by engine/ward-works; the work orders remain the record of truth. A ward with no "
     "works has no record (absence, not zero).", ["works_count", "paid_net_total"], "work_order"),
    ("procurement.ward_cell", "procurement",
     "One H3 cell carrying its WARD's tender count (tenders whose title names the ward) — a derived, rebuildable map "
     "layer written by engine/ward-works; the tenders remain the record of truth. A ward with no tenders has no record.",
     ["tenders_count"], "tender"),
]


def main():
    dev, steward = login(DEV_USER, DEV_REALM), login(STEWARD_USER)
    # x-subject is the entity AGGREGATED (work orders / tenders, per ward) — the cell is only the map unit, and
    # (cell, L3, measure) is already city.cell_profile (ADR-0060 §3a: one subject × rung × kind = one noun).
    for sid, domain, desc, values, subject in SCHEMAS:
        try:
            call(NETWORK + "/v1/schema-requests", {"schema_id": sid, "version": VER, "definition": defn(sid, domain, desc, values, subject),
                 "sensitivity": "internal", "justification": "Ward Works: public works and tenders by ward on the city map."}, dev)
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise SystemExit("FAIL (%s) %s: %s" % (e.code, sid, (e.read() or b"")[:300].decode(errors="replace")))
        call(NETWORK + "/v1/schema-requests/decide", {"schema_id": sid, "version": VER, "decision": "approve"}, steward)
        print("ok %s@%s governed" % (sid, VER))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
