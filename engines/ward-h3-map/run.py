#!/usr/bin/env python3
"""ward-h3-map — map each AOI H3 cell to the WARD whose boundary contains its centroid → space.ward_cell.

The ward→H3 step of City Setup: after a ward boundary (space.ward, uploaded via ward-ingest) is present, this
assigns every governed cell to a ward so any cell.* / demographics.* measure can be aggregated per ward.
Pure point-in-polygon (ray casting) over the tenant's existing cells — reads the demographics.population grid
(full AOI) or falls back to cell.buildings. NO external deps, NO egress; all I/O via the broker run-token.

  Env: TENANT/WRITE_TENANT · RUN_TOKEN
"""
import json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT", "")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
METHOD = "ward-h3-map@0.1.0"


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=90).read())


def query(scope):
    return [r.get("object", r) for r in call("/v1/query", {"scope": scope}).get("results", [])]


def write(scope, oid, obj):
    obj = {k: v for k, v in obj.items() if v is not None}
    call("/v1/write", {"scope": scope, "id": oid, "object": obj, "valid_time": {"from": NOW}, "asserted_at": NOW})


def _pip(pt, ring):
    """Ray-casting point-in-polygon for one ring of [lng,lat] pairs."""
    x, y = pt; inside = False; n = len(ring); j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]; xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _contains(geom, pt):
    """GeoJSON Polygon / MultiPolygon contains pt (respecting holes)."""
    t = (geom or {}).get("type"); c = (geom or {}).get("coordinates") or []
    if t == "Polygon":
        polys = [c]
    elif t == "MultiPolygon":
        polys = c
    else:
        return False
    for poly in polys:
        if not poly:
            continue
        if _pip(pt, poly[0]) and not any(_pip(pt, hole) for hole in poly[1:]):
            return True
    return False


def _bbox(geom):
    xs, ys = [], []
    def walk(a):
        if a and isinstance(a[0], (int, float)):
            xs.append(a[0]); ys.append(a[1])
        else:
            for b in a: walk(b)
    walk((geom or {}).get("coordinates") or [])
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — refusing to guess.")
    wards = [w for w in query("space.ward") if w.get("geometry")]
    if not wards:
        print("[ward-h3-map] no space.ward uploaded yet — upload a ward boundary at City Setup first."); print('RESULT: {"wards":0,"cells":0}'); return 0
    # cells: prefer the full-AOI population grid; else built-up cells
    cells = []
    for c in query("demographics.population"):
        sp = c.get("space") or {}; h = sp.get("h3"); ctr = sp.get("centroid")
        if h and ctr: cells.append((h, ctr))
    if not cells:
        for c in query("cell.buildings"):
            cr = c.get("cell_ref") or {}; h = cr.get("h3_id"); ctr = cr.get("centroid")
            if h and ctr: cells.append((h, ctr))
    if not cells:
        raise SystemExit("FATAL: no cell grid (demographics.population / cell.buildings) to map wards onto.")
    res = int(os.environ.get("RES", "9"))
    wb = [(w, _bbox(w["geometry"])) for w in wards]
    written = 0; per = {}
    for h, ctr in cells:
        x, y = ctr[0], ctr[1]
        for w, (x0, y0, x1, y1) in wb:
            if x < x0 or x > x1 or y < y0 or y > y1:
                continue
            if _contains(w["geometry"], (x, y)):
                wn = str(w.get("ward_no")); nm = w.get("name")
                oid = f"ds:{TENANT}:space/ward_cell/{h}"
                write("space.ward_cell", oid, {"schema": "space.ward_cell", "id": oid, "tenant": TENANT,
                      "ward_no": wn, "ward_name": nm, "h3_id": h, "resolution": res, "method": METHOD, "observed_at": NOW})
                written += 1; per[wn] = per.get(wn, 0) + 1
                break
    print("[ward-h3-map] mapped %d cells across %d wards: %s" % (written, len(wards),
          ", ".join("%s=%d" % (k, v) for k, v in sorted(per.items())[:20])), flush=True)
    print("RESULT: " + json.dumps({"wards": len(wards), "cells_mapped": written}))


if __name__ == "__main__":
    main()
