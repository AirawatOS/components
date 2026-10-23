#!/usr/bin/env python3
"""gazetteer-builder — build the per-tenant GEOCODING GAZETTEER into the registry (space.place), for ANY city.

Generic + AOI-driven: fetch an OSM extract (by URL, or an uploaded PBF), clip it to the tenant's AOI bbox, and
derive three geocode tiers, each resolved to a point + H3 res-9 cell:
  • locality — OSM place nodes (suburb/neighbourhood/village/town/locality/hamlet/quarter) with a name
  • pincode  — centroid of all OSM features carrying addr:postcode (6-digit), per code
  • ward     — centroid of each space.ward boundary already in the tenant (ward-ingest)
Written as governed `space.place` facts (NOT a file) — tenant-scoped, queryable, idempotent (deterministic ids).
`water-complaints` reads space.place to place complaints (colony→pin→ward fallback). Real OSM only; no synthetic
data; fails loudly if the AOI is missing or no PBF source is given.

  Env: TENANT · AOI_ID (opt) · OSM_PBF_URL (https) | OSM_PBF_FILE_ID (uploaded) · RES (default 9)
Runtime: geospatial (osmium + h3).
"""
import base64, json, os, re, shutil, subprocess, tempfile, time, urllib.parse, urllib.request

TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""     # never guess the tenant
AOI_HINT = os.environ.get("AOI_ID", "")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/osm"
PLACE_VALUES = {"suburb", "neighbourhood", "village", "town", "locality", "hamlet", "quarter", "city_block"}
_PIN = re.compile(r"^\d{6}$")
_TIER_CONF = {"locality": 0.8, "pincode": 0.5, "ward": 0.3}


def call(path, body):
    req = urllib.request.Request(os.environ["AIRAWAT_API"] + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": os.environ["RUN_TOKEN"]}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def unwrap(res):
    rs = res.get("results") if isinstance(res, dict) else None
    if rs:
        return rs[0].get("object") or rs[0]
    return res.get("object") if isinstance(res, dict) else res


def _norm(s):
    """IDENTICAL to water-complaints._norm — the lookup keys MUST match the consumer."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def resolve_aoi():
    if AOI_HINT:
        o = unwrap(call("/v1/read", {"scope": "space.aoi", "id": AOI_HINT}))
        if not isinstance(o, dict) or len(o.get("bbox") or []) != 4:
            raise SystemExit(f"FATAL: space.aoi {AOI_HINT} missing / no 4-el bbox")
        return [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), AOI_HINT
    rows = call("/v1/query", {"scope": "space.aoi"}).get("results") or []
    mine = [(r.get("object") or r) for r in rows if len(((r.get("object") or r).get("bbox") or [])) == 4]
    if not mine:
        raise SystemExit(f"FATAL: no space.aoi for tenant '{TENANT}' — run City Setup first. No default AOI.")
    o = max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))
    return [float(v) for v in o["bbox"]], int(o.get("h3_res", 9)), o["id"]


# ── obtain the PBF (URL download via declared egress, or an uploaded asset) ─────────────────────────────────
def _download(url):
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not u.hostname:
        raise SystemExit(f"FATAL: OSM_PBF_URL must be https (got {url!r})")
    if not (u.hostname.endswith("geofabrik.de") or u.hostname.endswith("openstreetmap.org")):
        raise SystemExit(f"FATAL: OSM_PBF_URL host {u.hostname} is not an allowed OSM source (declared egress).")
    tf = tempfile.NamedTemporaryFile(suffix=".osm.pbf", delete=False)
    req = urllib.request.Request(url, headers={"User-Agent": "AirawatOS/gazetteer-builder (+https://airawat.org)"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        shutil.copyfileobj(r, tf)
    tf.close()
    print(f"  downloaded {os.path.getsize(tf.name)//1024//1024} MB from {u.hostname} in {time.time()-t0:.0f}s", flush=True)
    return tf.name


def _from_asset(fid):
    raw = base64.b64decode(call("/v1/file/get", {"id": fid}).get("content_b64") or "")
    if not raw:
        raise SystemExit(f"FATAL: PBF asset {fid} is empty / not in the filestore")
    tf = tempfile.NamedTemporaryFile(suffix=".osm.pbf", delete=False); tf.write(raw); tf.close()
    print(f"  read uploaded PBF asset {fid[:20]}… ({len(raw)//1024//1024} MB)", flush=True)
    return tf.name


def _clip_to_bbox(src, bbox):
    """osmium extract the AOI bbox (ref-complete) before the expensive locations pass — large regions otherwise
    blow the subprocess cap. No-op if the CLI is missing or the file is already small."""
    try:
        if not shutil.which("osmium") or os.path.getsize(src) < 150 * 1024 * 1024:
            return src
    except Exception:
        return src
    w, s, e, n = bbox; pad = 0.03; dst = src + ".clip.osm.pbf"
    try:
        subprocess.run(["osmium", "extract", "-b", f"{w-pad},{s-pad},{e+pad},{n+pad}", src, "-o", dst,
                        "--overwrite", "-s", "complete_ways"], check=True, capture_output=True, timeout=150)
        print(f"  clipped to AOI bbox → {os.path.getsize(dst)//1024//1024} MB", flush=True)
        return dst
    except Exception as ex:
        print(f"  clip skipped ({str(ex)[:80]}) — reading full PBF", flush=True)
        return src


# ── parse OSM → localities + pincode centroids (within bbox) ────────────────────────────────────────────────
def parse_osm(pbf_path, bbox):
    import osmium
    w, s, e, n = bbox
    localities = {}                       # norm_name -> [lon, lat, name]
    pin_acc = {}                          # pin -> [sum_lon, sum_lat, count]

    def _inside(lon, lat):
        return w <= lon <= e and s <= lat <= n

    def _pin_of(tags):
        p = (tags.get("addr:postcode") or "").strip()
        return p if _PIN.match(p) else None

    class _H(osmium.SimpleHandler):
        def node(self, nd):
            loc = nd.location
            if not loc.valid() or not _inside(loc.lon, loc.lat):
                return
            t = nd.tags
            if t.get("place") in PLACE_VALUES and t.get("name"):
                k = _norm(t.get("name"))
                if len(k) >= 3 and k not in localities:
                    localities[k] = [loc.lon, loc.lat, t.get("name")]
            p = _pin_of(t)
            if p:
                a = pin_acc.setdefault(p, [0.0, 0.0, 0]); a[0] += loc.lon; a[1] += loc.lat; a[2] += 1

        def way(self, way):
            p = _pin_of(way.tags)
            if not p:
                return
            xs = ys = 0.0; c = 0
            try:
                for nd in way.nodes:
                    if nd.location.valid():
                        xs += nd.location.lon; ys += nd.location.lat; c += 1
            except Exception:
                return
            if c and _inside(xs / c, ys / c):
                a = pin_acc.setdefault(p, [0.0, 0.0, 0]); a[0] += xs / c; a[1] += ys / c; a[2] += 1

    _H().apply_file(pbf_path, locations=True)
    pincodes = {p: [a[0] / a[2], a[1] / a[2]] for p, a in pin_acc.items() if a[2]}
    return localities, pincodes


# ── ward centroids from space.ward (pure-python shoelace) ───────────────────────────────────────────────────
def _ring_centroid(ring):
    nn = len(ring)
    if nn < 3:
        xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
        return (sum(xs) / len(xs), sum(ys) / len(ys)) if xs else (None, None)
    a = cx = cy = 0.0
    for i in range(nn):
        x0, y0 = ring[i][0], ring[i][1]; x1, y1 = ring[(i + 1) % nn][0], ring[(i + 1) % nn][1]
        cr = x0 * y1 - x1 * y0; a += cr; cx += (x0 + x1) * cr; cy += (y0 + y1) * cr
    if abs(a) < 1e-12:
        xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
        return sum(xs) / nn, sum(ys) / nn
    a *= 0.5
    return cx / (6 * a), cy / (6 * a)


def _poly_centroid(geom):
    t = (geom or {}).get("type"); c = (geom or {}).get("coordinates") or []
    rings = [c[0]] if (t == "Polygon" and c) else ([poly[0] for poly in c if poly] if t == "MultiPolygon" else [])
    if not rings:
        return None, None
    biggest = max(rings, key=lambda r: abs(sum(r[i][0] * r[(i + 1) % len(r)][1] - r[(i + 1) % len(r)][0] * r[i][1]
                                               for i in range(len(r)))))
    return _ring_centroid(biggest)


def ward_centroids():
    out = {}                               # norm_name -> [lon, lat, name]
    for r in call("/v1/query", {"scope": "space.ward"}).get("results") or []:
        o = r.get("object") or r
        name = o.get("name") or o.get("ward_name")
        lon, lat = _poly_centroid(o.get("geometry") or {})
        if name and lon is not None:
            k = _norm(name)
            if len(k) >= 3:
                out[k] = [lon, lat, name]
    return out


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — refusing to guess.")
    import h3
    bbox, res, aoi_ref = resolve_aoi()
    res = int(os.environ.get("RES", res or 9))

    url = os.environ.get("OSM_PBF_URL", "").strip()
    fid = os.environ.get("OSM_PBF_FILE_ID", "").strip()
    if not (url or fid):
        raise SystemExit("FATAL: give OSM_PBF_URL (https Geofabrik/OSM extract) or OSM_PBF_FILE_ID (uploaded). "
                         "No synthetic data.")
    pbf = _download(url) if url else _from_asset(fid)
    localities, pincodes = ({}, {})
    try:
        clipped = _clip_to_bbox(pbf, bbox)
        localities, pincodes = parse_osm(clipped, bbox)
    finally:
        for p in (pbf, pbf + ".clip.osm.pbf"):
            try: os.unlink(p)
            except Exception: pass
    wards = ward_centroids()
    print(f"[gazetteer-builder] derived {len(localities)} localities · {len(pincodes)} pincodes · "
          f"{len(wards)} wards (AOI {aoi_ref}, res {res})", flush=True)

    rows = []
    def _emit(kind, key, lon, lat, name=None):
        slug = re.sub(r"[^a-z0-9]+", "-", key).strip("-") or "x"
        oid = f"ds:{TENANT}:space/place/{kind}-{slug}"
        rows.append({"schema": "space.place", "id": oid, "tenant": TENANT, "kind": kind, "key": key,
                     "name": name, "pincode": key if kind == "pincode" else None,
                     "centroid": [round(lon, 6), round(lat, 6)], "h3": h3.latlng_to_cell(lat, lon, res),
                     "resolution": res, "tier_confidence": _TIER_CONF[kind], "source": SRC, "observed_at": NOW})
    for k, (lon, lat, name) in localities.items(): _emit("locality", k, lon, lat, name)
    for p, (lon, lat) in pincodes.items():         _emit("pincode", p, lon, lat, p)
    for k, (lon, lat, name) in wards.items():       _emit("ward", k, lon, lat, name)

    if not rows:
        raise SystemExit("FATAL: no gazetteer entries derived for this AOI — nothing written (no fabrication).")
    for r in rows:
        r = {k: v for k, v in r.items() if v is not None}
        call("/v1/write", {"scope": "space.place", "id": r["id"], "object": r,
                           "valid_time": {"from": NOW}, "asserted_at": NOW})
    by_kind = {"locality": len(localities), "pincode": len(pincodes), "ward": len(wards)}
    print(f"[gazetteer-builder] wrote {len(rows)} space.place (by kind: {by_kind})", flush=True)
    print("RESULT: " + json.dumps({"space_place": len(rows), "by_kind": by_kind, "aoi": aoi_ref}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
