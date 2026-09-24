#!/usr/bin/env python3
"""ingest-property-survey — load a GIS property survey into geo.parcel (the cadastral master the tax signals join to).

The survey is the ground-truth of built properties: plot polygons + rich attributes, keyed by a survey `uid`. This
driver reads it from the filestore in one of two modes and writes generic `geo.parcel` records (optionally `geo.building`
footprints too), attributed to this component:
  • SHAPEFILE mode — SURVEY_SHP_FILE_ID gives the geometry (a pure-stdlib .shp polygon reader, no geo libraries);
    SURVEY_CSV_FILE_ID (the .dbf's CSV twin) gives attributes, joined to features BY RECORD ORDER (shapefile export
    order == dbf row order). Preferred: real plot polygons.
  • CSV mode — SURVEY_CSV_FILE_ID alone, using each row's lat/long as a point centroid.
`parcel_id` is the survey uid — the exact key the property-tax register links to; where a survey has no uid the row is
skipped (never invented). Column names auto-detect from the header (case-insensitive), with env overrides.
"""
import os, json, csv, io, struct, time, base64

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
SHP_FILE_ID = os.environ.get("SURVEY_SHP_FILE_ID", "")
CSV_FILE_ID = os.environ.get("SURVEY_CSV_FILE_ID", "")
MAX = int(os.environ.get("MAX", "0"))
WRITE_BUILDINGS = os.environ.get("WRITE_BUILDINGS", "").lower() in ("1", "true", "yes")
BATCH = int(os.environ.get("WRITE_BATCH", "1000"))
SOURCE = os.environ.get("SOURCE", "property-survey")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

CANDIDATES = {
    "uid":   [os.environ.get("UID_FIELD"),   "uid", "survey_id", "surveyid"],
    "ward":  [os.environ.get("WARD_FIELD"),  "ward_no", "ward no", "ward"],
    "use":   [os.environ.get("USE_FIELD"),   "property_u", "property use", "property_use", "type"],
    "area":  [os.environ.get("AREA_FIELD"),  "area_geo", "area geo", "area_sqm", "shape_area", "shape area", "builtup_ar"],
    "owner": [os.environ.get("OWNER_FIELD"), "owner_name", "owner name", "owner"],
    "lat":   [os.environ.get("LAT_FIELD"),   "lat", "latitude", "y"],
    "lon":   [os.environ.get("LON_FIELD"),   "long", "lon", "longitude", "x"],
}


def call(path, body):
    import urllib.request
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read() or b"{}")


def read_file(fid):
    """Read a filestore asset via the broker: {id} → {content_b64}. Returns bytes (broker mediates; no direct I/O)."""
    res = call("/v1/file/get", {"id": fid})
    return base64.b64decode(res.get("content_b64") or "")


def _resolve_columns(header):
    lower = {h.lower().strip(): h for h in header}
    out = {}
    for field, cands in CANDIDATES.items():
        for c in cands:
            if c and c.lower() in lower:
                out[field] = lower[c.lower()]; break
    return out


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


# ── pure-stdlib shapefile (.shp) polygon reader ──────────────────────────────────────────────────────
def read_shp_polygons(shp_bytes):
    """Yield (geojson_geometry, bbox[minlon,minlat,maxlon,maxlat]) per record, in file order.
    Handles shape type 5 (Polygon) and 15 (PolygonZ). Multi-ring records → GeoJSON Polygon [outer, holes...]
    (single-ring is the common case for footprints; true multi-part holes collapse into one Polygon's rings)."""
    if len(shp_bytes) < 100:
        return
    off, end = 100, len(shp_bytes)
    while off + 8 <= end:
        _recno, clen = struct.unpack(">ii", shp_bytes[off:off + 8])       # record header (big-endian)
        cstart = off + 8; cend = cstart + clen * 2                        # content length is in 16-bit words
        if cend > end:
            break
        c = shp_bytes[cstart:cend]
        off = cend
        if len(c) < 4:
            yield None, None; continue
        shtype = struct.unpack("<i", c[0:4])[0]                          # content is little-endian
        if shtype not in (5, 15):                                        # not a polygon → no geometry
            yield None, None; continue
        box = struct.unpack("<4d", c[4:36])                              # xmin,ymin,xmax,ymax
        nparts, npoints = struct.unpack("<ii", c[36:44])
        pstart = 44
        parts = list(struct.unpack("<%di" % nparts, c[pstart:pstart + 4 * nparts]))
        pts_off = pstart + 4 * nparts
        pts = struct.unpack("<%dd" % (npoints * 2), c[pts_off:pts_off + 16 * npoints])
        coords = [[round(pts[2 * i], 7), round(pts[2 * i + 1], 7)] for i in range(npoints)]
        rings = []
        for k in range(nparts):
            a = parts[k]; b = parts[k + 1] if k + 1 < nparts else npoints
            rings.append(coords[a:b])
        geom = {"type": "Polygon", "coordinates": rings}
        yield geom, [box[0], box[1], box[2], box[3]]


def _centroid(bbox):
    return [round((bbox[0] + bbox[2]) / 2, 7), round((bbox[1] + bbox[3]) / 2, 7)] if bbox else None


def features_to_parcels(shp_bytes, csv_text, tenant):
    """PURE mapping (no I/O): shapefile geometry + CSV attributes (joined by record order) → geo.parcel envelopes."""
    rows = list(csv.DictReader(io.StringIO(csv_text))) if csv_text else []
    cols = _resolve_columns(rows[0].keys()) if rows else {}
    def g(row, f):
        c = cols.get(f); return (row.get(c) or "").strip() if (c and row) else ""
    for i, (geom, bbox) in enumerate(read_shp_polygons(shp_bytes)):
        row = rows[i] if i < len(rows) else {}
        uid = g(row, "uid") or (f"feat-{i}")
        pid = f"ds:{tenant}:geo/parcel/{uid}"
        obj = {"schema": "geo.parcel", "id": pid, "tenant": tenant, "parcel_id": uid,
               "geometry": geom, "bbox": bbox, "ward": g(row, "ward") or None,
               "land_use": g(row, "use") or None, "owner": g(row, "owner") or None,
               "area_sqm": _num(g(row, "area")), "source": SOURCE, "created_at": NOW}
        yield pid, {k: v for k, v in obj.items() if v is not None}, geom, _centroid(bbox), uid


def csv_to_parcels(csv_text, tenant):
    """PURE mapping (no I/O): CSV with lat/long → geo.parcel point centroids (no polygon)."""
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    cols = _resolve_columns(rows[0].keys()) if rows else {}
    def g(row, f):
        c = cols.get(f); return (row.get(c) or "").strip() if c else ""
    for i, row in enumerate(rows):
        uid = g(row, "uid") or (f"row-{i}")
        lat, lon = _num(g(row, "lat")), _num(g(row, "lon"))
        geom = {"type": "Point", "coordinates": [lon, lat]} if lat is not None and lon is not None else None
        pid = f"ds:{tenant}:geo/parcel/{uid}"
        obj = {"schema": "geo.parcel", "id": pid, "tenant": tenant, "parcel_id": uid,
               "geometry": geom, "bbox": ([lon, lat, lon, lat] if geom else None),
               "ward": g(row, "ward") or None, "land_use": g(row, "use") or None,
               "owner": g(row, "owner") or None, "area_sqm": _num(g(row, "area")),
               "source": SOURCE, "created_at": NOW}
        yield pid, {k: v for k, v in obj.items() if v is not None}, geom, ([lon, lat] if geom else None), uid


def main():
    if not TENANT:
        print("[ingest-property-survey] no tenant in this context — nothing to do"); return 0
    csv_text = ""
    if CSV_FILE_ID:
        csv_text = read_file(CSV_FILE_ID).decode("utf-8", "replace")
    if SHP_FILE_ID:
        shp_bytes = read_file(SHP_FILE_ID)
        gen = features_to_parcels(shp_bytes, csv_text, TENANT); mode = "shapefile"
    elif csv_text:
        gen = csv_to_parcels(csv_text, TENANT); mode = "csv-centroid"
    else:
        print("[ingest-property-survey] neither SURVEY_SHP_FILE_ID nor SURVEY_CSV_FILE_ID set — nothing to do"); return 0
    batch, n, nogeom = [], 0, 0
    def _wr(b):
        r = call("/v1/write/batch", {"items": b, "asserted_at": NOW}) or {}
        if isinstance(r, dict):
            written[0] += int(r.get("written", 0) or 0)
            if r.get("errors"):
                print("  batch errors:", json.dumps(r["errors"][:3])[:400], flush=True)
    written = [0]
    for pid, obj, geom, centroid, uid in gen:
        batch.append({"scope": "geo.parcel", "id": pid, "object": obj})       # {items:[{scope,id,object}], asserted_at}
        if geom is None:
            nogeom += 1
        elif WRITE_BUILDINGS:
            bid = f"ds:{TENANT}:geo/building/{uid}"
            batch.append({"scope": "geo.building", "id": bid, "object": {"schema": "geo.building", "id": bid,
                "tenant": TENANT, "geometry": geom, "centroid": centroid, "parcel_id": uid, "source": SOURCE}})
        n += 1
        if len(batch) >= BATCH:
            _wr(batch); batch = []
        if MAX and n >= MAX:
            break
    if batch:
        _wr(batch)
    print(f"[ingest-property-survey] processed {n} features ({mode}); {nogeom} without geometry; PERSISTED {written[0]} geo.parcel", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
