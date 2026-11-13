#!/usr/bin/env python3
"""osm-facilities driver — CRITICAL FACILITIES for an AOI (ADR-0060 L1, Wave-1 physical foundation).

Individual points of interest — schools, hospitals, clinics, police & fire stations, govt offices, utilities,
places of worship, markets, transport hubs — extracted from the tenant's uploaded OSM PBF via osmium (the same
reliable, Overpass-free path as osm-roads). Each POI is written as a `geo.facility` (native Point geometry)
tagged with the H3 cell it sits in, so City Scan can pull one cell's facilities on drill-in and count them.

POIs come from OSM `amenity` / `healthcare` / `emergency` / `power` / `man_made` tags on BOTH nodes and ways
(a hospital mapped as a building way → its centroid). Deterministic natural-key id (…/facility/<type><osm_id>)
so re-runs upsert. Real OSM only — fails loudly if the AOI or the PBF asset is missing. NO synthetic fallback,
NO tenant/AOI default.

  Env: TENANT · AOI_ID (opt; else discover) · OSM_PBF_FILE_ID (filestore digest of the uploaded PBF, injected
       by the scheduler from the driver's component-asset)
"""
import base64, json, os, shutil, subprocess, sys, tempfile, time, urllib.request

AOI_HINT = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""   # NO default — never guess the tenant
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/osm"

# OSM tag (key,value) -> (category, subcategory). Real critical-facility taxonomy; extend by adding rows.
POI_FILTERS = {
    ("amenity", "school"): ("education", "school"),
    ("amenity", "college"): ("education", "college"),
    ("amenity", "university"): ("education", "university"),
    ("amenity", "kindergarten"): ("education", "kindergarten"),
    ("amenity", "library"): ("education", "library"),
    ("amenity", "hospital"): ("healthcare", "hospital"),
    ("amenity", "clinic"): ("healthcare", "clinic"),
    ("amenity", "doctors"): ("healthcare", "clinic"),
    ("amenity", "pharmacy"): ("healthcare", "pharmacy"),
    ("amenity", "dentist"): ("healthcare", "clinic"),
    ("healthcare", "hospital"): ("healthcare", "hospital"),
    ("healthcare", "clinic"): ("healthcare", "clinic"),
    ("healthcare", "centre"): ("healthcare", "clinic"),
    ("amenity", "police"): ("emergency", "police"),
    ("amenity", "fire_station"): ("emergency", "fire_station"),
    ("emergency", "fire_station"): ("emergency", "fire_station"),
    ("amenity", "townhall"): ("government", "townhall"),
    ("amenity", "courthouse"): ("government", "courthouse"),
    ("amenity", "public_building"): ("government", "public_building"),
    ("office", "government"): ("government", "govt_office"),
    ("amenity", "place_of_worship"): ("religious", "place_of_worship"),
    ("amenity", "marketplace"): ("commerce", "marketplace"),
    ("amenity", "bus_station"): ("transport", "bus_station"),
    ("railway", "station"): ("transport", "railway_station"),
    ("power", "substation"): ("utility", "substation"),
    ("power", "plant"): ("utility", "power_plant"),
    ("man_made", "water_tower"): ("utility", "water_tower"),
    ("man_made", "water_works"): ("utility", "water_works"),
    ("man_made", "wastewater_plant"): ("utility", "wastewater_plant"),
    # 0.2.0 — the Public Services / Points of Interest split (category keys kept stable for the analyst + city profile;
    # display names live in Visualize). Service delivery points:
    ("healthcare", "laboratory"): ("healthcare", "laboratory"),
    ("amenity", "nursing_home"): ("healthcare", "nursing_home"),
    ("emergency", "ambulance_station"): ("emergency", "ambulance_station"),
    ("amenity", "post_office"): ("government", "post_office"),
    ("amenity", "toilets"): ("sanitation", "toilets"),
    ("amenity", "drinking_water"): ("sanitation", "drinking_water"),
    ("amenity", "waste_disposal"): ("sanitation", "waste_disposal"),
    ("amenity", "waste_transfer_station"): ("sanitation", "waste_transfer_station"),
    ("amenity", "recycling"): ("sanitation", "recycling"),
    ("amenity", "fuel"): ("transport", "fuel"),
    ("amenity", "charging_station"): ("transport", "ev_charging"),
    ("amenity", "bank"): ("banking", "bank"),
    ("amenity", "atm"): ("banking", "atm"),
    ("amenity", "community_centre"): ("welfare", "community_centre"),
    ("amenity", "social_facility"): ("welfare", "social_facility"),
    ("amenity", "social_centre"): ("welfare", "social_centre"),
    # other points of interest:
    ("shop", "mall"): ("commerce", "mall"),
    ("shop", "supermarket"): ("commerce", "supermarket"),
    ("shop", "department_store"): ("commerce", "department_store"),
    ("tourism", "museum"): ("religious", "museum"),
    ("amenity", "theatre"): ("religious", "theatre"),
    ("amenity", "arts_centre"): ("religious", "arts_centre"),
    ("leisure", "park"): ("recreation", "park"),
    ("leisure", "playground"): ("recreation", "playground"),
    ("leisure", "sports_centre"): ("recreation", "sports_centre"),
    ("leisure", "stadium"): ("recreation", "stadium"),
    ("leisure", "garden"): ("recreation", "garden"),
    ("man_made", "mast"): ("utility", "telecom_mast"),
    ("tower:type", "communication"): ("utility", "telecom_tower"),
}
# 0.2.0 — which categories are SERVICE DELIVERY POINTS (the city is accountable; access gaps matter) vs other points of
# interest. Consumers (Visualize, the analyst) group by this; the record keeps the stable category key.
SERVICE_CATEGORIES = {"healthcare", "education", "emergency", "government", "sanitation", "transport", "banking", "welfare"}


def call(path, body):
    req = urllib.request.Request(os.environ["AIRAWAT_API"] + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": os.environ["RUN_TOKEN"]}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def unwrap(res):
    return res.get("object") if isinstance(res, dict) and isinstance(res.get("object"), dict) else res


def resolve_aoi():
    if AOI_HINT:
        ht = AOI_HINT.split(":")[1] if AOI_HINT.count(":") >= 2 else ""
        if ht and ht != TENANT:
            raise SystemExit(f"FATAL: AOI_ID {AOI_HINT} belongs to tenant '{ht}', not write tenant '{TENANT}'.")
        o = unwrap(call("/v1/read", {"scope": "space.aoi", "id": AOI_HINT}))
        if not isinstance(o, dict) or len(o.get("bbox") or []) != 4:
            raise SystemExit(f"FATAL: space.aoi {AOI_HINT} missing / no 4-el bbox: {o!r}")
        return o, [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), AOI_HINT
    rows = call("/v1/query", {"scope": "space.aoi"}).get("results") or []
    mine = [(r.get("object") or r) for r in rows
            if str((r.get("object") or r).get("id", "")).startswith(f"ds:{TENANT}:")
            and len((r.get("object") or r).get("bbox") or []) == 4]
    if not mine:
        raise SystemExit(f"FATAL: no space.aoi for tenant '{TENANT}' — run City Setup first. No default AOI.")
    o = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))
    return o, [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), o["id"]


def _match(tags):
    for (k, v), cat in POI_FILTERS.items():
        if tags.get(k) == v:
            return cat
    return None


def pois_from_pbf(pbf_path, bbox):
    """All facility POIs inside the bbox from a local OSM PBF (nodes + way-centroids). Returns
    [{osm_type,osm_id,lon,lat,category,subcategory,name}]. Needs pyosmium in the runtime."""
    import osmium
    w, s, e, n = bbox
    out = []

    class _H(osmium.SimpleHandler):
        def __init__(self):
            super().__init__(); self._c = 0

        def node(self, nd):
            cat = _match(nd.tags)
            if not cat:
                return
            loc = nd.location
            if not loc.valid() or not (w <= loc.lon <= e and s <= loc.lat <= n):
                return
            out.append({"osm_type": "n", "osm_id": nd.id, "lon": loc.lon, "lat": loc.lat,
                        "category": cat[0], "subcategory": cat[1], "name": nd.tags.get("name")})
            self._bump()

        def way(self, way):
            # facilities mapped as buildings/areas (e.g. a hospital polygon) → centroid of the way's nodes
            cat = _match(way.tags)
            if not cat:
                return
            xs = ys = 0.0; pts = 0
            try:
                for nd in way.nodes:
                    if nd.location.valid():
                        xs += nd.location.lon; ys += nd.location.lat; pts += 1
            except Exception:
                return
            if not pts:
                return
            lon, lat = xs / pts, ys / pts
            if not (w <= lon <= e and s <= lat <= n):
                return
            out.append({"osm_type": "w", "osm_id": way.id, "lon": lon, "lat": lat,
                        "category": cat[0], "subcategory": cat[1], "name": way.tags.get("name")})
            self._bump()

        def _bump(self):
            self._c += 1
            if self._c % 20000 == 0:
                print(f"  …matched {self._c} facilities so far", flush=True)

    h = _H()
    h.apply_file(pbf_path, locations=True)   # locations=True → resolve node coords from the PBF (for way centroids)
    return out


def _clip_to_bbox(src_path, bbox):
    """If the PBF is large and the `osmium` CLI is present, extract a reference-complete slice for the AOI bbox
    (with a small pad) BEFORE the expensive locations=True pass. A regional/zone PBF (300 MB–1.5 GB) otherwise
    forces pyosmium to index every node in the country and blows the 180s host-subprocess cap (HTTP 504). osmium
    extract uses a spatial index and clips in seconds; ways crossing the edge keep all their nodes (reference-
    complete) so way centroids stay correct. No-op (returns src) when the CLI is missing or the file is small —
    the driver still works, just without the speed-up. See ADR-0060 / Wave-1."""
    try:
        if not shutil.which("osmium") or os.path.getsize(src_path) < 150 * 1024 * 1024:
            return src_path
    except Exception:
        return src_path
    w, s, e, n = bbox
    pad = 0.03
    dst = src_path + ".clip.osm.pbf"
    box = f"{w-pad},{s-pad},{e+pad},{n+pad}"
    try:
        t0 = time.time()
        subprocess.run(["osmium", "extract", "-b", box, src_path, "-o", dst, "--overwrite", "-s", "complete_ways"],
                       check=True, capture_output=True, timeout=150)
        mb = os.path.getsize(dst) // 1024 // 1024
        print(f"  clipped PBF to AOI bbox {box} → {mb} MB in {time.time()-t0:.0f}s (osmium extract)", flush=True)
        return dst
    except Exception as ex:
        print(f"  osmium extract clip skipped ({str(ex)[:80]}) — reading full PBF", flush=True)
        return src_path


def pois_from_asset(pbf_id, bbox):
    res = call("/v1/file/get", {"id": pbf_id})
    raw = base64.b64decode(res.get("content_b64") or "")
    if not raw:
        raise SystemExit(f"FATAL: PBF asset {pbf_id} is empty / not in the filestore")
    tf = tempfile.NamedTemporaryFile(suffix=".osm.pbf", delete=False)
    clipped = None
    try:
        tf.write(raw); tf.close()
        print(f"reading facilities from uploaded PBF asset {pbf_id[:20]}… ({len(raw)//1024//1024} MB) via osmium", flush=True)
        path = _clip_to_bbox(tf.name, bbox)
        clipped = path if path != tf.name else None
        return pois_from_pbf(path, bbox)
    finally:
        for p in (tf.name, clipped):
            if p:
                try: os.unlink(p)
                except Exception: pass


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant. Refusing to guess.")
    import h3
    aoi, bbox, res, aoi_ref = resolve_aoi()
    pbf_id = os.environ.get("OSM_PBF_FILE_ID", "").strip()
    if not pbf_id:
        raise SystemExit("FATAL: OSM_PBF_FILE_ID not set — upload the regional OSM PBF via the driver's Settings "
                         "(Connections). No Overpass fallback / no synthetic data.")
    pois = pois_from_asset(pbf_id, bbox)
    print(f"osmium matched {len(pois)} facility POIs in the AOI (from the uploaded PBF)", flush=True)
    if not pois:
        raise SystemExit("FATAL: no facility POIs found in the PBF for this AOI — nothing written (no fabrication).")

    written = 0
    by_cat = {}
    for p in pois:
        lon, lat = round(p["lon"], 6), round(p["lat"], 6)
        hcell = h3.latlng_to_cell(lat, lon, res)
        fid = f"ds:{TENANT}:facility/{p['category']}/{p['osm_type']}{p['osm_id']}"
        fact = {
            "schema": "geo.facility", "id": fid, "tenant": TENANT,
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "h3_id": hcell, "resolution": res, "centroid": [lon, lat],
            "category": p["category"], "subcategory": p["subcategory"],
            "name": p.get("name"),
            "method": "osm-pbf", "source_ref": SRC, "layer": "L1",
            "source": {"aoi": aoi_ref, "driver": "osm-facilities", "osm": f"{p['osm_type']}/{p['osm_id']}"},
            "identifiers": [{"scheme": "osm", "value": f"{p['osm_type']}/{p['osm_id']}"}],
            "prov": {"method": "osm-pbf", "source_ref": SRC, "observed_at": NOW},
            "observed_at": NOW}
        call("/v1/write", {"scope": "geo.facility", "id": fid, "object": fact,
                           "valid_time": {"from": NOW}, "asserted_at": NOW})
        written += 1
        by_cat[p["category"]] = by_cat.get(p["category"], 0) + 1

    print(f"wrote {written} geo.facility POIs from the OSM PBF (real OSM)")
    print(f"  by category: {json.dumps(by_cat, sort_keys=True)}")
    print(f"RESULT: {json.dumps({'source': 'osm-pbf', 'geo_facility': written, 'by_category': by_cat})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
