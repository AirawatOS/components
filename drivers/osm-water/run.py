#!/usr/bin/env python3
"""osm-water driver — the city's LAKES, reservoirs, ponds / tanks and wetlands from the tenant's OpenStreetMap PBF.

Emits L1 native geometry into the existing governed `geo.osm_feature` schema (no new noun), as osm-transit does,
distinguished by `feature` = lake | reservoir | pond | wetland | water. Closed ways AND multipolygon relations
(Bellandur and Varthur are relations) become Polygon / MultiPolygon via pyosmium's area assembly. Kept: named water
bodies of at least MIN_NAMED_HA, and unnamed ones of at least MIN_UNNAMED_HA (rivers, canals and stream areas are
not lakes and are left out). The area (m²) and a stable kind travel in tags (_area_m2, _water_kind).

Its ONLY I/O is the kernel broker: read the tenant's space.aoi · read the uploaded PBF (filestore) · write
geo.osm_feature in batches. Deterministic natural-key ids (ds:<tenant>:geo/osm_feature/<way|relation>/<osm_id>) so
re-runs upsert. Feeds driver/lake-watch, which measures each water body's extent over the years from satellites.

Inputs (env): AOI_ID (optional) · TENANT · OSM_PBF_FILE_ID (the regional PBF from City Setup) · MIN_NAMED_HA · MIN_UNNAMED_HA
"""
import json, math, os, shutil, subprocess, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                     # bundled ids.py (container)
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_common"))

AOI_HINT = os.environ.get("AOI_ID", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
AOI_REF = ""
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
VIA = "ds:airawat:driver/osm-water"
MIN_NAMED_HA = float(os.environ.get("MIN_NAMED_HA", "0.5"))
MIN_UNNAMED_HA = float(os.environ.get("MIN_UNNAMED_HA", "2"))
NOT_LAKES = {"river", "canal", "stream", "stream_pool", "ditch", "drain", "wastewater", "lock", "moat", "fountain",
             "swimming_pool", "oxbow", "rapids", "fish_pass"}
KEEP_TAGS = ("name", "name:en", "name:kn", "alt_name", "water", "natural", "landuse", "wetland", "wikidata", "operator", "salt")


def call(path, body):
    req = urllib.request.Request(os.environ["AIRAWAT_API"] + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Run-Token": os.environ["RUN_TOKEN"]}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def unwrap(res):
    if isinstance(res, dict):
        if "object" in res and isinstance(res["object"], dict):
            return res["object"]
        for k in ("body", "data", "result", "response"):
            if k in res:
                return unwrap(res[k])
        return res
    if isinstance(res, (bytes, str)):
        text = res.decode("utf-8") if isinstance(res, bytes) else res
        try: return json.loads(text)
        except (ValueError, TypeError): return text
    return res


def resolve_aoi():
    if AOI_HINT:
        ht = AOI_HINT.split(":")[1] if AOI_HINT.count(":") >= 2 else ""
        if ht and ht != TENANT:
            raise SystemExit(f"FATAL: AOI_ID {AOI_HINT} belongs to '{ht}', not write tenant '{TENANT}'.")
        o = unwrap(call("/v1/read", {"scope": "space.aoi", "id": AOI_HINT}))
        if not isinstance(o, dict) or len(o.get("bbox") or []) != 4:
            raise SystemExit(f"FATAL: space.aoi {AOI_HINT} missing / no 4-el bbox: {o!r}")
        return o, [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), AOI_HINT
    rows = call("/v1/query", {"scope": "space.aoi"}).get("results") or []
    mine = [(r.get("object") or r) for r in rows
            if str((r.get("object") or r).get("id", "")).startswith(f"ds:{TENANT}:")
            and len((r.get("object") or r).get("bbox") or []) == 4]
    if not mine:
        raise SystemExit(f"FATAL: no space.aoi for tenant '{TENANT}' — run City Setup first.")
    o = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))
    return o, [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), o["id"]



def water_kind(t):
    """The water body's kind from its tags, or None if it is not a lake-like body."""
    w = t.get("water"); nat = t.get("natural"); lu = t.get("landuse")
    if w in NOT_LAKES or t.get("waterway") or t.get("leisure") == "swimming_pool" or t.get("amenity") == "fountain":
        return None
    if nat == "wetland":
        return "wetland"
    if w == "reservoir" or lu == "reservoir":
        return "reservoir"
    if w == "lake" or lu == "basin" and w is None and "lake" in (t.get("name") or "").lower():
        return "lake"
    if w in ("pond", "tank", "basin") or lu == "basin":
        return "pond"
    if nat == "water":
        n = (t.get("name") or "").lower()
        return "lake" if ("lake" in n or "kere" in n or "tank" in n) else "water"
    return None


def area_m2(rings):
    """Planar area (m²) of [outer, inner…] rings of [lon, lat] — equirectangular at the ring's latitude; city scale."""
    tot = 0.0
    for k, ring in enumerate(rings):
        if len(ring) < 4:
            continue
        lat0 = sum(p[1] for p in ring) / len(ring); kx = 111320.0 * math.cos(math.radians(lat0)); ky = 110540.0
        a = 0.0
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            a += (x1 * kx) * (y2 * ky) - (x2 * kx) * (y1 * ky)
        tot += abs(a) / 2.0 * (1 if k == 0 else -1)
    return max(tot, 0.0)


def _clip_to_bbox(src_path, bbox):
    """osmium-extract a reference-complete AOI slice from a large regional PBF BEFORE the locations=True pass
    (else pyosmium indexes the whole country and blows the host subprocess cap). No-op if CLI missing / small."""
    try:
        if not shutil.which("osmium") or os.path.getsize(src_path) < 150 * 1024 * 1024:
            return src_path
    except Exception:
        return src_path
    w, s, e, n = bbox; pad = 0.03; dst = src_path + ".clip.osm.pbf"
    try:
        t0 = time.time()
        subprocess.run(["osmium", "extract", "-b", f"{w-pad},{s-pad},{e+pad},{n+pad}", src_path,
                        "-o", dst, "--overwrite", "-s", "smart"], check=True, capture_output=True, timeout=150)
        print(f"  clipped PBF to AOI → {os.path.getsize(dst)//1024//1024} MB in {time.time()-t0:.0f}s", flush=True)
        return dst
    except Exception as ex:
        print(f"  osmium clip skipped ({str(ex)[:80]}) — reading full PBF", flush=True); return src_path



def water_from_pbf(pbf_path, bbox):
    """Lake-like AREAS (closed ways + multipolygon relations) whose ring centre lies in the bbox → elements
    {otype, id, tags, polygons: [[outer, inner…], …]} via pyosmium's area assembler."""
    import osmium
    w, s, e, n = bbox; els = []
    class _H(osmium.SimpleHandler):
        def area(self, a):
            t = {k.k: k.v for k in a.tags}
            kind = water_kind(t)
            if not kind:
                return
            polys = []
            try:
                for outer in a.outer_rings():
                    ring = [[nd.lon, nd.lat] for nd in outer]
                    holes = [[[nd.lon, nd.lat] for nd in inner] for inner in a.inner_rings(outer)]
                    if len(ring) >= 4:
                        polys.append([ring] + [h for h in holes if len(h) >= 4])
            except Exception:
                return
            if not polys:
                return
            cx = sum(p[0] for p in polys[0][0]) / len(polys[0][0]); cy = sum(p[1] for p in polys[0][0]) / len(polys[0][0])
            if not (w <= cx <= e and s <= cy <= n):
                return
            els.append({"otype": "way" if a.from_way() else "relation", "id": a.orig_id(), "tags": t, "kind": kind, "polygons": polys})
    _H().apply_file(pbf_path, locations=True, idx="flex_mem")
    return els


def fetch_water_from_asset(pbf_id, bbox):
    import base64, tempfile
    raw = base64.b64decode((call("/v1/file/get", {"id": pbf_id}) or {}).get("content_b64") or "")
    if not raw:
        raise SystemExit(f"FATAL: PBF asset {pbf_id} is empty / not in the filestore")
    tf = tempfile.NamedTemporaryFile(suffix=".osm.pbf", delete=False); clipped = None
    try:
        tf.write(raw); tf.close()
        print(f"reading water bodies from uploaded PBF {pbf_id[:20]}… ({len(raw)//1024//1024} MB) via osmium", flush=True)
        path = _clip_to_bbox(tf.name, bbox); clipped = path if path != tf.name else None
        return water_from_pbf(path, bbox)
    finally:
        for p in (tf.name, clipped):
            if p:
                try: os.unlink(p)
                except Exception: pass


def to_feature(el):
    """One lake-like area → one geo.osm_feature (Polygon / MultiPolygon), or None below the size thresholds."""
    import ids
    t = el["tags"]; a = sum(area_m2(p) for p in el["polygons"]); name = t.get("name") or t.get("name:en")
    if a < (MIN_NAMED_HA if name else MIN_UNNAMED_HA) * 10000:
        return None
    r6 = lambda ring: [[round(x, 6), round(y, 6)] for x, y in ring]
    polys = [[r6(r) for r in p] for p in el["polygons"]]
    geometry = ({"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1
                else {"type": "MultiPolygon", "coordinates": polys})
    tags = {k: v for k, v in t.items() if k in KEEP_TAGS}
    tags["_water_kind"] = el["kind"]; tags["_area_m2"] = str(int(round(a)))
    nkey = "osm_way" if el["otype"] == "way" else "osm_relation"
    return ids.entity(TENANT, "geo/osm_feature/" + el["otype"], "geo.osm_feature",
        natural=[(nkey, str(el["id"]), "OpenStreetMap")],
        extra={"tenant": TENANT, "osm_id": str(el["id"]), "osm_type": el["otype"], "feature": el["kind"],
               "tags": tags, "geometry": geometry, "source": {"aoi": AOI_REF, "via": VIA}})


def main():
    global AOI_REF
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant.")
    aoi, bbox, res, AOI_REF = resolve_aoi()
    print(f"tenant={TENANT} · AOI {AOI_REF} · bbox={bbox}", flush=True)
    pbf_id = os.environ.get("OSM_PBF_FILE_ID", "").strip()
    if not pbf_id:
        raise SystemExit("FATAL: OSM_PBF_FILE_ID not set — upload the regional OSM PBF in City Setup (water areas need "
                         "the full multipolygon assembly a PBF gives; there is no Overpass fallback).")
    els = fetch_water_from_asset(pbf_id, bbox)
    print(f"osmium assembled {len(els)} lake-like areas in the AOI", flush=True)
    written = small = 0; kinds = {}; named = 0; batch = []
    def _flush():
        nonlocal written
        if not batch: return
        r = call("/v1/write/batch", {"items": batch, "asserted_at": NOW}) or {}
        written += int(r.get("written", 0))
        if r.get("errors"): print("  batch errors (%s): %s" % (r.get("error_count", len(r["errors"])), json.dumps(r["errors"][:2])[:300]), flush=True)
        batch.clear()
    for el in els:
        f = to_feature(el)
        if f is None:
            small += 1; continue
        batch.append({"scope": "geo.osm_feature", "id": f["id"], "object": f})
        kinds[f["feature"]] = kinds.get(f["feature"], 0) + 1; named += bool(f["tags"].get("name"))
        if len(batch) >= 500: _flush()
    _flush()
    if written == 0:
        raise SystemExit("FATAL: no lake-like water bodies written for this AOI")
    print(f"wrote {written} water bodies ({named} named); {small} below the size thresholds", flush=True)
    print("by kind:", json.dumps(dict(sorted(kinds.items()))), flush=True)
    print("RESULT: " + json.dumps({"geo_osm_feature": written, "named": named, "by_kind": kinds}))


if __name__ == "__main__":
    main()
