#!/usr/bin/env python3
"""ADR-0103/0060 — govern `space.place`: a registry-backed GAZETTEER entry (name/PIN/ward → point + H3).

The gazetteer-builder writes these per tenant (AOI-bounded) so geocoding reference data lives in the governed
registry, not a filestore blob. `water-complaints` reads them to place complaints on the H3 grid. Permissive
(additionalProperties:true) so it can never reject a builder's row. Run on the network host, e.g.:
    cat components/tools/govern_space_place.py | ssh <host> 'docker exec -i airawat-network python -'
"""
import os, json, urllib.request
NET = os.environ.get("NETWORK_URL", "http://localhost:8090")
DEFINITION = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "space.place",
    "type": "object",
    "description": ("A gazetteer entry: a named place / postal area / ward resolved to a point + H3 cell, used to "
                    "geocode records by name or PIN. Produced per tenant (AOI-bounded) by the gazetteer-builder."),
    "properties": {
        "kind":       {"type": "string", "enum": ["locality", "pincode", "ward"],
                       "description": "Which geocode TIER this entry serves."},
        "key":        {"type": "string", "description": "Lookup key — normalised name (locality/ward) or 6-digit PIN."},
        "name":       {"type": "string", "description": "Original (display) name."},
        "pincode":    {"type": "string"},
        "centroid":   {"type": "array", "items": {"type": "number"}, "description": "[longitude, latitude]."},
        "h3":         {"type": "string", "description": "H3 cell (resolution per `resolution`)."},
        "resolution": {"type": "integer"},
        "tier_confidence": {"type": "number", "description": "Geocode confidence when matched at this tier."},
        "source":     {"type": "string"},
        "observed_at": {"type": "string"},
    },
    "required": ["kind", "key", "centroid", "h3"],
    "additionalProperties": True,
    "x-subject": "place",
}
def main():
    body = {"schema_id": "space.place", "version": "0.1.0", "definition": DEFINITION, "sensitivity": "internal"}
    req = urllib.request.Request(NET + "/v1/schemas", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    print(json.dumps(json.loads(urllib.request.urlopen(req, timeout=10).read() or b"{}"), indent=2))
if __name__ == "__main__":
    main()
