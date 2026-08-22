#!/usr/bin/env python3
"""osm-roads driver: OSM highway ways for an AOI bbox → one geo.osm_feature per way.

Confined (>= L1) — its ONLY I/O is the kernel broker:
  read   the space.aoi frame (the bbox to fill; default ds:kmc:space/aoi/kanpur-core)
  fetch  the Overpass interpreter (egress-gated to overpass-api.de)
  write  one geo.osm_feature per way, with a deterministic natural-key id
         (ds:<tenant>:geo/osm_feature/way/<osm_id>) so re-runs upsert, not duplicate.

Each feature carries its road class (the OSM `highway` tag → `feature`), the full
tag set, and a GeoJSON LineString geometry in WGS84 [lon, lat] order, built from the
way's node geometry. Ways with fewer than two geometry points are skipped (counted).
Fails loudly if the AOI is missing or the Overpass fetch returns nothing parseable.

Inputs (env):
  AOI_ID        space.aoi id to read        (default ds:kmc:space/aoi/kanpur-core)
  TENANT        tenant namespace for writes (default kmc)
  OVERPASS_URL  interpreter endpoint        (default https://overpass-api.de/api/interpreter)
"""
import json, os, sys, urllib.request
sys.path.insert(0, os.path.join(   # components/drivers/osm-roads → up 3 → components → /_common
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_common"))
import ids   # shared id-derivation helper (ADR-0063): id from the natural key + governed identifiers[]

AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/kanpur-core")
TENANT = os.environ.get("TENANT", "kmc")
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")


def call(path, body):
    """The one and only I/O primitive: POST JSON through the kernel broker."""
    req = urllib.request.Request(
        os.environ["AIRAWAT_API"] + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Run-Token": os.environ["RUN_TOKEN"]},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def unwrap(res):
    """Peel broker envelopes; parse string bodies that are actually JSON."""
    if isinstance(res, dict):
        if "object" in res and isinstance(res["object"], dict):
            return res["object"]
        for k in ("body", "data", "result", "response"):
            if k in res:
                return unwrap(res[k])
        return res
    if isinstance(res, (bytes, str)):
        text = res.decode("utf-8") if isinstance(res, bytes) else res
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text
    return res


def read_aoi(aoi_id):
    obj = unwrap(call("/v1/read", {"scope": "space.aoi", "id": aoi_id}))
    bbox = obj.get("bbox") if isinstance(obj, dict) else None
    if not bbox or len(bbox) != 4:
        raise SystemExit(f"FATAL: space.aoi {aoi_id} not found or has no 4-element bbox: {obj!r}")
    return obj, [float(v) for v in bbox]


def fetch_highway_ways(bbox):
    """Query Overpass for all ways carrying a `highway` tag inside the bbox.

    OSM bbox is [west, south, east, north]; Overpass wants (south,west,north,east).
    `out tags geom` returns each way with its tags and inline node coordinates.
    """
    west, south, east, north = bbox
    ql = (
        "[out:json][timeout:120];"
        f'way["highway"]({south},{west},{north},{east});'
        "out tags geom;"
    )
    payload = unwrap(call("/v1/fetch", {"url": OVERPASS_URL, "method": "POST", "body": ql}))
    elements = payload.get("elements") if isinstance(payload, dict) else None
    if elements is None:
        raise SystemExit(f"FATAL: Overpass fetch returned no 'elements' array: {str(payload)[:500]}")
    return [e for e in elements if e.get("type") == "way"]


def to_feature(el):
    """One OSM way → one geo.osm_feature; None if the way can't become a line."""
    geom = el.get("geometry") or []
    coords = [[float(p["lon"]), float(p["lat"])] for p in geom
              if isinstance(p, dict) and "lon" in p and "lat" in p]
    if len(coords) < 2:
        return None
    tags = el.get("tags") or {}
    osm_id = str(el["id"])
    # The OSM id is the way's NATURAL KEY: it drives the ds: <local> (deterministic → re-runs upsert) AND is
    # recorded in identifiers[] so a road is resolvable by its OSM id (ADR-0063). `entity()` does both.
    return ids.entity(TENANT, "geo/osm_feature/way", "geo.osm_feature",
        natural=[("osm_way", osm_id, "OpenStreetMap")],
        extra={
            "tenant": TENANT,
            "osm_id": osm_id,
            "osm_type": "way",
            "feature": tags.get("highway", "unknown"),   # road class from the highway tag
            "tags": tags,
            "geometry": {"type": "LineString", "coordinates": coords},
            "source": {"aoi": AOI_ID, "endpoint": OVERPASS_URL, "via": "ds:airawat:driver/osm-roads"},
        })


def main():
    aoi, bbox = read_aoi(AOI_ID)
    print(f"aoi {AOI_ID} ({aoi.get('name', '?')}) bbox={bbox}")

    ways = fetch_highway_ways(bbox)
    print(f"overpass returned {len(ways)} highway ways")

    written = skipped = 0
    classes = {}
    for el in ways:
        feat = to_feature(el)
        if feat is None:
            skipped += 1
            continue
        call("/v1/write", {"scope": "geo.osm_feature", "id": feat["id"], "object": feat})
        written += 1
        classes[feat["feature"]] = classes.get(feat["feature"], 0) + 1

    print(f"wrote {written} geo.osm_feature objects; skipped {skipped} (insufficient geometry)")
    print("road classes:", json.dumps(dict(sorted(classes.items())), ensure_ascii=False))
    if written == 0:
        raise SystemExit("FATAL: no usable highway ways were written for this AOI")


if __name__ == "__main__":
    main()
