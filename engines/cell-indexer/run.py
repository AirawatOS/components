#!/usr/bin/env python3
"""cell-indexer: the L1→L2 KEYSTONE engine (ADR-0060).

Confined — its only I/O is the kernel broker. It READS an AOI (space.aoi) for the bbox + default H3
resolution and any L1 FEATURE layer, and INDEXES that layer into an L2 `cell.*` indicator, writing one
cell-measure per touched cell. It generalizes station-interpolation (IDW over station points) to *any*
feature over cells, with four aggregation methods:

  fraction  area fraction of a cell covered by matching polygons — computed by sampling each cell's
            fine H3 children (res+2) against the polygons' polyfill (no shapely needed). e.g. built-up.
  mean      mean of a numeric field over point features falling in the cell.            e.g. LST → heat.
  count     number of point features in the cell.
  density   count per km² of cell area.

Every output carries `derived_from` (the contributing L1 feature ids — real lineage, ADR-0041) and
layer:"L2". "H3 is not the source of truth": the cell references the L1 features it was derived from; the
native geometry is never discarded. The output is only as real as its inputs.

Config (env, injected clean by the host via --set KEY=VALUE):
  AOI_ID           the space.aoi to index over                         (default a demo AOI)
  INPUT_SCHEMA     the L1 layer noun to read                           (e.g. geo.landcover)
  OUTPUT_SCHEMA    the cell.* noun to write                            (e.g. cell.builtup)
  METHOD           fraction | mean | count | density                   (default fraction)
  CLASS_FIELD      for fraction: the feature field to filter on        (e.g. lc_class)
  CLASS_VALUE      for fraction: the value that counts                 (e.g. built)
  VALUE_FIELD      for mean: the numeric feature field                 (e.g. lst_value)
  RESOLUTION       H3 res override (else the AOI's h3_res, else 9)
  WRITE_TENANT     (default kmc)
"""
import json, os, math, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/demo")
INPUT_SCHEMA = os.environ.get("INPUT_SCHEMA", "geo.landcover")
OUTPUT_SCHEMA = os.environ.get("OUTPUT_SCHEMA", "cell.builtup")
METHOD = os.environ.get("METHOD", "fraction")
CLASS_FIELD = os.environ.get("CLASS_FIELD", "lc_class")
CLASS_VALUE = os.environ.get("CLASS_VALUE", "built")
VALUE_FIELD = os.environ.get("VALUE_FIELD", "value")
RES_OVERRIDE = os.environ.get("RESOLUTION")
WRITE_TENANT = os.environ.get("WRITE_TENANT", "kmc")
CID = os.environ.get("AIROS_COMPONENT_ID", "ds:airawat:engine/cell-indexer")
CVER = os.environ.get("AIROS_COMPONENT_VERSION", "0.1.0")
_CAP = 50                                                   # cap derived_from list per cell (lineage, bounded)

import h3


def _call(path, body):
    tok = os.environ["RUN_TOKEN"]
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": tok}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


# ---------------------------------------------------------------- geometry → H3 (no shapely)
def _ring_latlng(coords):
    """GeoJSON ring [[lng,lat],…] → [(lat,lng),…] for h3.LatLngPoly."""
    return [(c[1], c[0]) for c in coords]


def geojson_to_cells(geom, res):
    """A GeoJSON Polygon/MultiPolygon → the set of res cells covering it. Robust across h3 builds."""
    if not geom:
        return set()
    t = geom.get("type"); co = geom.get("coordinates") or []
    polys = []                                              # each: (outer_ring, [holes])
    if t == "Polygon":
        polys = [(co[0], co[1:])]
    elif t == "MultiPolygon":
        polys = [(p[0], p[1:]) for p in co]
    else:
        return set()
    cells = set()
    for outer, holes in polys:
        try:
            poly = h3.LatLngPoly(_ring_latlng(outer), *[_ring_latlng(h) for h in holes])
            cells |= set(h3.polygon_to_cells(poly, res))
        except Exception:
            for c in outer:                                # degenerate fallback: at least the vertices' cells
                cells.add(h3.latlng_to_cell(c[1], c[0], res))
    return cells


def _feature_point(o):
    """Best-effort [lat,lng] for a point-ish feature."""
    if o.get("latitude") is not None and o.get("longitude") is not None:
        return o["latitude"], o["longitude"]
    sp = o.get("space") or {}
    c = sp.get("centroid") or o.get("centroid")
    if c and len(c) == 2:
        return c[1], c[0]                                  # stored [lng,lat]
    return None


def cells_in_bbox(bbox, res):
    w, s, e, n = bbox
    try:
        poly = h3.LatLngPoly([(s, w), (s, e), (n, e), (n, w)])
        return set(h3.polygon_to_cells(poly, res))
    except Exception:
        cells = set(); step = h3.average_hexagon_edge_length(res, unit="km") / 111.0 / 2.0
        lat = s
        while lat <= n:
            lng = w
            while lng <= e:
                cells.add(h3.latlng_to_cell(lat, lng, res)); lng += step
            lat += step
        return cells


# ---------------------------------------------------------------- the aggregation core (pure; self-testable)
def aggregate(features, cells, res, method):
    """features: list of dicts (each with 'id' and geometry-or-point). Returns {cell: (value, conf, [src_ids])}."""
    out = {}
    if not features:
        return out                                           # NO source data → write NOTHING (never fabricate a
                                                             # complete grid of zeros — that would hide a fetch failure)
    if method == "fraction":
        child_res = min(res + 2, 15)
        covered = {}                                        # cell -> set(fine children covered by matching polys)
        contributors = {}                                   # cell -> [feature ids]
        for f in features:
            if CLASS_FIELD and CLASS_VALUE and str(f.get(CLASS_FIELD)) != CLASS_VALUE:
                continue
            fine = geojson_to_cells(f.get("geometry"), child_res)
            for fc in fine:
                parent = h3.cell_to_parent(fc, res)
                covered.setdefault(parent, set()).add(fc)
                c = contributors.setdefault(parent, [])
                if f.get("id") and f["id"] not in c and len(c) < _CAP:
                    c.append(f["id"])
        for cell in cells:
            cov = covered.get(cell)
            if not cov:
                out[cell] = (0.0, 0.8, [])                   # COMPLETE grid: 0-fill cells with no matching feature
                continue                                     # (land cover covers the whole AOI → absence is a real 0)
            total = len(h3.cell_to_children(cell, child_res)) or 1
            frac = min(1.0, len(cov) / total)
            out[cell] = (round(frac, 4), 0.85, contributors.get(cell, []))
        return out
    if method == "idw":
        # INTERPOLATED complete grid: every AOI cell gets an inverse-distance-weighted value from the point
        # observations (e.g. coarse LST → a smooth per-cell heat surface). MODELLED, not measured — the output
        # carries method=idw + a distance-based confidence so the map/trust report shows it's derived.
        pts = []
        for f in features:
            p = _feature_point(f); v = f.get(VALUE_FIELD)
            if p and isinstance(v, (int, float)):
                pts.append((p[0], p[1], float(v)))
        if not pts:
            return {}
        for cell in cells:
            clat, clng = h3.cell_to_latlng(cell)
            kx = math.cos(math.radians(clat)) * 111.32
            num = den = 0.0; dmin = 1e9
            for (plat, plng, v) in pts:
                d = math.hypot((plng - clng) * kx, (plat - clat) * 111.32)   # km, equirectangular
                if d < 1e-6:
                    num, den = v, 1.0; dmin = 0.0; break     # sample sits in the cell → exact
                w = 1.0 / (d * d)
                num += w * v; den += w; dmin = min(dmin, d)
            if den > 0:
                conf = round(max(0.05, min(0.9, 1.0 / (1.0 + dmin))), 3)      # nearer an obs → more confident
                out[cell] = (round(num / den, 3), conf, [])
        return out
    # point-based methods
    bycell = {}                                             # cell -> ([values],[ids])
    for f in features:
        p = _feature_point(f)
        if not p:
            continue
        cell = h3.latlng_to_cell(p[0], p[1], res)
        if cell not in cells:
            continue
        v = f.get(VALUE_FIELD)
        vals, ids = bycell.setdefault(cell, ([], []))
        if method == "mean" and isinstance(v, (int, float)):
            vals.append(float(v))
        elif method in ("count", "density"):
            vals.append(1.0)
        if f.get("id") and len(ids) < _CAP:
            ids.append(f["id"])
    area_km2 = h3.average_hexagon_area(res, unit="km^2") if method == "density" else None
    for cell, (vals, ids) in bycell.items():
        if method == "mean":
            if not vals:
                continue
            value = round(sum(vals) / len(vals), 3); conf = round(1 - 1 / (1 + len(vals)), 3)
        elif method == "count":
            value = len(vals); conf = 1.0
        else:                                               # density
            value = round(len(vals) / area_km2, 3); conf = 1.0
        out[cell] = (value, conf, ids)
    if method in ("count", "density"):                       # COMPLETE grid: 0-fill cells with no points
        for cell in cells:
            out.setdefault(cell, (0, 1.0, []))
    return out


def _boundary(hcell):
    return [[round(lo, 6), round(la, 6)] for (la, lo) in h3.cell_to_boundary(hcell)]


# ---------------------------------------------------------------- broker mode
def main():
    aoi = _call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object")
    if not aoi:
        print(f"FAIL: no AOI at {AOI_ID}"); return 1
    bbox = aoi["bbox"]; res = int(RES_OVERRIDE) if RES_OVERRIDE else int(aoi.get("h3_res", 9))
    rows = _call("/v1/query", {"scope": INPUT_SCHEMA, "limit": 20000}).get("results", [])
    features = [(r.get("object") or r) for r in rows]
    cells = cells_in_bbox(bbox, res)
    print(f"AOI '{aoi.get('name', AOI_ID)}' bbox={bbox} res={res} → {len(cells)} cells; "
          f"{len(features)} {INPUT_SCHEMA} features; method={METHOD} → {OUTPUT_SCHEMA}")

    result = aggregate(features, cells, res, METHOD)
    if not result:
        print(f"no {INPUT_SCHEMA} features intersect the AOI cells — nothing written "
              f"(the output is only as real as its inputs).")
        return 0

    obs = "1970-01-01T00:00:00Z"
    for f in features:
        if f.get("observed_at"):
            obs = max(obs, f["observed_at"])
    obs = obs if obs != "1970-01-01T00:00:00Z" else _call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object", {}).get("observed_at", "2025-01-01T00:00:00Z")

    written = 0
    for hcell, (value, conf, src) in result.items():
        lat, lng = h3.cell_to_latlng(hcell)
        rid = f"ds:{WRITE_TENANT}:{OUTPUT_SCHEMA.replace('.', '/')}/{hcell}"
        fact = {"schema": OUTPUT_SCHEMA, "id": rid,
                "cell_ref": {"h3_id": hcell, "resolution": res,
                             "centroid": [round(lng, 5), round(lat, 5)], "boundary": _boundary(hcell)},
                "value": value, "method": METHOD, "confidence": conf,
                "derived_from": src or [f"{INPUT_SCHEMA}@aoi:{AOI_ID}"],
                "layer": "L2",
                "produced_by": {"component": {"id": CID, "version": CVER}},
                # prov: the derivation block the house rule requires on a measure (ADR-0041/0063 R5) — packages
                # the lineage this engine already computes (method + inputs + producer + confidence + time).
                "prov": {"method": METHOD, "confidence": conf,
                         "derived_from": src or [f"{INPUT_SCHEMA}@aoi:{AOI_ID}"],
                         "produced_by": {"component": {"id": CID, "version": CVER}}, "observed_at": obs},
                "observed_at": obs}
        _call("/v1/write", {"scope": OUTPUT_SCHEMA, "id": rid, "object": fact,
                            "valid_time": {"from": obs}, "asserted_at": obs})
        written += 1
    print(f"indexed {written} {OUTPUT_SCHEMA} cells from {len(features)} {INPUT_SCHEMA} features "
          f"(method={METHOD}, res={res}); each carries derived_from + layer=L2")
    return 0


# ---------------------------------------------------------------- self-test (no broker; proves the math)
def selftest():
    res = 9
    # a small square polygon (~ a few cells) marked built, and a point layer for mean
    sq = [[80.20, 26.40], [80.25, 26.40], [80.25, 26.45], [80.20, 26.45], [80.20, 26.40]]
    poly = {"type": "Polygon", "coordinates": [sq]}
    feats = [{"id": "ds:kmc:geo/landcover/1", "lc_class": "built", "geometry": poly},
             {"id": "ds:kmc:geo/landcover/2", "lc_class": "water",
              "geometry": {"type": "Polygon", "coordinates": [[[80.30, 26.40], [80.32, 26.40], [80.32, 26.42], [80.30, 26.40]]]}}]
    cells = geojson_to_cells(poly, res) | geojson_to_cells(feats[1]["geometry"], res)
    frac = aggregate(feats, cells, res, "fraction")
    assert frac, "fraction produced no cells"
    assert all(0 <= v[0] <= 1 for v in frac.values()), "fraction out of range"     # COMPLETE grid: 0-fill allowed
    built = geojson_to_cells(poly, res)
    assert all("ds:kmc:geo/landcover/1" in frac[c][2] and frac[c][0] > 0 for c in built), "built cells missing/zero"
    # water poly must NOT contribute to a built-up fraction: its cells are present but ZERO-valued (complete grid)
    water_cells = geojson_to_cells(feats[1]["geometry"], res)
    assert all(frac.get(wc, (0,))[0] == 0 for wc in water_cells if wc not in built), "water leaked into built-up"
    # mean over points (LST-like) — place both at the same cell's centroid so they co-locate
    pcell = h3.latlng_to_cell(26.42, 80.22, res)
    plat, plng = h3.cell_to_latlng(pcell)
    pts = [{"id": "p1", "latitude": plat, "longitude": plng, "value": 40.0},
           {"id": "p2", "latitude": plat, "longitude": plng, "value": 44.0}]
    mean = aggregate(pts, {pcell}, res, "mean")
    got = mean.get(pcell)
    assert got and abs(got[0] - 42.0) < 2.0, f"mean wrong: {got}"
    # idw: interpolate the two co-located points over a small ring of cells → EVERY cell gets a value in-range
    ring = {pcell} | set(h3.grid_disk(pcell, 2))
    idw = aggregate(pts, ring, res, "idw")
    assert len(idw) == len(ring), "idw must fill every AOI cell"
    assert all(39.9 <= v[0] <= 44.1 for v in idw.values()), "idw values out of source range"
    assert idw[pcell][1] >= max(v[1] for c, v in idw.items() if c != pcell), "nearest cell should be most confident"
    print("SELFTEST OK:")
    print(f"  fraction: {len(frac)} built-up cells, values {sorted(round(v[0],3) for v in frac.values())[:6]}…, lineage present")
    print(f"  mean:     cell {pcell[:8]}… value={got[0]} conf={got[1]} from {got[2]}")
    print(f"  layer contract: every output carries derived_from + layer=L2")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(selftest() if "--selftest" in sys.argv else main())
