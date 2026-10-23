#!/usr/bin/env python3
"""build_gazetteer — produce the geocoding GAZETTEER that `water-complaints` consumes (GAZETTEER_FILE_ID).

It joins three OPEN-DATA sources into the one JSON the driver's layered geocoder reads:

    { "localities": { "<norm name>": [lon, lat, "<h3>"] },      # OSM place names (colony/locality tier)
      "pincodes":   { "<6-digit PIN>": [lon, lat, "<h3>"] },    # PIN-code centroids (PIN tier)
      "wards":      { "<norm name>": [lon, lat, "<h3>"] } }      # ward-boundary centroids (ward floor tier)

Each value is [longitude, latitude, H3-res-9-cell]. The H3 cell is PRECOMPUTED here (the confined L1 driver runtime
has no H3 library) so the driver just reads it. Keys for localities/wards are normalised EXACTLY as the driver
normalises them (lowercase, non-alphanumerics → single space, trimmed) so lookups match; PIN keys are the raw
6-digit code. This is an OFFLINE build tool (run once per city, dependency-light); the output is uploaded and
passed to the driver as GAZETTEER_FILE_ID. (It can later be certified as a driver on an H3-enabled runtime.)

Inputs (any subset — each tier is optional, but wards give guaranteed placement so include them):
  --osm-places FILE   OSM place names. .osm.pbf (needs `pyosmium`), OR a pre-extracted GeoJSON FeatureCollection
                      of Points, OR a CSV `name,lon,lat`. (Extract from a PBF with:
                      `osmium tags-filter city.osm.pbf n/place=suburb,neighbourhood,village,town,locality,hamlet
                       -o places.pbf` then this tool, or export to GeoJSON.)
  --wards FILE        ward/prabhag boundary GeoJSON (same file ward-ingest uses). Centroid per feature.
  --pins FILE         PIN-code centroids: CSV `pin,lon,lat` or GeoJSON Points with a `pincode`/`pin` property.
  --out FILE          output gazetteer JSON (default: gazetteer.json)
  --res N             H3 resolution (default 9, matching water.hotspot)
  --name-key K        place/locality name property (default: name)
  --ward-name-key K   ward name property (default: tries name/ward_name/WARD_NAME/prabhag)

Example:
  python build_gazetteer.py --osm-places delhi-places.geojson --wards delhi-wards.geojson \\
      --pins delhi-pins.csv --out gazetteer.delhi.json
"""
import os, sys, json, csv, re, argparse

try:
    import h3 as _h3
except Exception:
    _h3 = None


def _cell(lat, lon, res):
    """H3 res-N cell for (lat, lon) — supports h3 v4 (latlng_to_cell) and v3 (geo_to_h3)."""
    if _h3 is None:
        return None
    if hasattr(_h3, "latlng_to_cell"):
        return _h3.latlng_to_cell(lat, lon, res)      # h3 >= 4
    return _h3.geo_to_h3(lat, lon, res)               # h3 3.x


def _norm(s):
    """IDENTICAL to water-complaints._norm — the lookup keys MUST match the driver's."""
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def _entry(lon, lat, res):
    c = _cell(lat, lon, res)
    return [round(float(lon), 6), round(float(lat), 6), c] if c else None


# ── source readers ────────────────────────────────────────────────────────────────────────────────────────
def _read_points(path, name_keys):
    """Yield (name, lon, lat) from a .pbf (pyosmium), a GeoJSON Point FeatureCollection, or a CSV name,lon,lat."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pbf":
        try:
            import osmium
        except Exception:
            raise SystemExit("reading .pbf needs pyosmium — `pip install osmium`, or pre-extract to GeoJSON/CSV.")
        PLACES = {"suburb", "neighbourhood", "village", "town", "locality", "hamlet", "city_block", "quarter"}
        out = []

        class H(osmium.SimpleHandler):
            def node(self, n):
                t = n.tags
                if "place" in t and t.get("place") in PLACES and t.get("name"):
                    out.append((t.get("name"), n.location.lon, n.location.lat))
        H().apply_file(path)
        yield from out
        return
    if ext in (".geojson", ".json"):
        gj = json.load(open(path, encoding="utf-8"))
        for f in (gj.get("features") or []):
            g = f.get("geometry") or {}
            if g.get("type") != "Point":
                continue
            props = f.get("properties") or {}
            name = next((props[k] for k in name_keys if props.get(k)), None)
            lon, lat = (g.get("coordinates") or [None, None])[:2]
            if name and lon is not None:
                yield name, lon, lat
        return
    # CSV name,lon,lat (header optional)
    with open(path, encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) < 3:
                continue
            name, lon, lat = row[0], row[1], row[2]
            try:
                yield name, float(lon), float(lat)
            except ValueError:
                continue   # header / bad line


def _ring_centroid(ring):
    """Area-weighted centroid of one [ [lon,lat], … ] ring (shoelace). Falls back to vertex mean if degenerate."""
    n = len(ring)
    if n < 3:
        xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
        return (sum(xs) / len(xs), sum(ys) / len(ys)) if xs else (None, None)
    a = cx = cy = 0.0
    for i in range(n):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        cross = x0 * y1 - x1 * y0
        a += cross; cx += (x0 + x1) * cross; cy += (y0 + y1) * cross
    if abs(a) < 1e-12:
        xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
        return sum(xs) / n, sum(ys) / n
    a *= 0.5
    return cx / (6 * a), cy / (6 * a)


def _poly_centroid(geom):
    """Centroid of a GeoJSON Polygon/MultiPolygon — the outer ring of its LARGEST part."""
    t = geom.get("type"); c = geom.get("coordinates") or []
    rings = []
    if t == "Polygon" and c:
        rings = [c[0]]
    elif t == "MultiPolygon":
        rings = [poly[0] for poly in c if poly]
    if not rings:
        return None, None

    def _area(r):
        a = 0.0
        for i in range(len(r)):
            x0, y0 = r[i][0], r[i][1]; x1, y1 = r[(i + 1) % len(r)][0], r[(i + 1) % len(r)][1]
            a += x0 * y1 - x1 * y0
        return abs(a)
    biggest = max(rings, key=_area)
    return _ring_centroid(biggest)


# ── build ─────────────────────────────────────────────────────────────────────────────────────────────────
def build(osm_places=None, wards=None, pins=None, res=9, name_key="name",
          ward_name_key=None):
    gaz = {"localities": {}, "pincodes": {}, "wards": {}}

    if osm_places:
        n = 0
        for name, lon, lat in _read_points(osm_places, [name_key, "name"]):
            e = _entry(lon, lat, res); k = _norm(name)
            if e and len(k) >= 3 and k not in gaz["localities"]:
                gaz["localities"][k] = e; n += 1
        print(f"[gazetteer] localities: {n} from {osm_places}")

    if wards:
        gj = json.load(open(wards, encoding="utf-8"))
        wkeys = [ward_name_key] if ward_name_key else ["name", "ward_name", "WARD_NAME", "Ward_Name", "prabhag", "WARD"]
        wkeys = [k for k in wkeys if k]
        n = 0
        for f in (gj.get("features") or []):
            props = f.get("properties") or {}
            name = next((props[k] for k in wkeys if props.get(k)), None)
            lon, lat = _poly_centroid(f.get("geometry") or {})
            if name and lon is not None:
                e = _entry(lon, lat, res); k = _norm(name)
                if e and len(k) >= 3:
                    gaz["wards"][k] = e; n += 1
        print(f"[gazetteer] wards: {n} from {wards}")

    if pins:
        n = 0
        ext = os.path.splitext(pins)[1].lower()
        if ext in (".geojson", ".json"):
            gj = json.load(open(pins, encoding="utf-8"))
            for f in (gj.get("features") or []):
                g = f.get("geometry") or {}; props = f.get("properties") or {}
                pin = str(props.get("pincode") or props.get("pin") or "").strip()
                lon, lat = (g.get("coordinates") or [None, None])[:2] if g.get("type") == "Point" else _poly_centroid(g)
                if re.fullmatch(r"\d{6}", pin) and lon is not None:
                    e = _entry(lon, lat, res)
                    if e:
                        gaz["pincodes"][pin] = e; n += 1
        else:
            with open(pins, encoding="utf-8") as fh:
                for row in csv.reader(fh):
                    if len(row) < 3:
                        continue
                    pin = str(row[0]).strip()
                    if not re.fullmatch(r"\d{6}", pin):
                        continue
                    try:
                        e = _entry(float(row[1]), float(row[2]), res)
                    except ValueError:
                        continue
                    if e:
                        gaz["pincodes"][pin] = e; n += 1
        print(f"[gazetteer] pincodes: {n} from {pins}")

    return gaz


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the water-complaints geocoding gazetteer from open data.")
    ap.add_argument("--osm-places"); ap.add_argument("--wards"); ap.add_argument("--pins")
    ap.add_argument("--out", default="gazetteer.json")
    ap.add_argument("--res", type=int, default=9)
    ap.add_argument("--name-key", default="name")
    ap.add_argument("--ward-name-key", default=None)
    a = ap.parse_args(argv)
    if not (a.osm_places or a.wards or a.pins):
        ap.error("give at least one of --osm-places / --wards / --pins")
    if _h3 is None:
        ap.error("H3 is required to compute cells — `pip install h3`. (Without it the gazetteer can't place complaints.)")
    gaz = build(a.osm_places, a.wards, a.pins, a.res, a.name_key, a.ward_name_key)
    json.dump(gaz, open(a.out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[gazetteer] wrote {a.out}: {len(gaz['localities'])} localities · "
          f"{len(gaz['pincodes'])} pincodes · {len(gaz['wards'])} wards (res {a.res}). "
          f"Upload it and pass its file id as GAZETTEER_FILE_ID to water-complaints.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
