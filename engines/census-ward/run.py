#!/usr/bin/env python3
"""Census Ward — the official 2011 census, by ward, on the city grid (writes demographics.census_ward).

Each H3 cell of a ward (space.ward_cell, from ward-h3-map) carries its ward's census population, area and density —
an OFFICIAL cross-check beside the GHSL 2020 model (demographics.population), which spreads people by built-up volume and
over-allocates commercial cores (staging: a Cottonpete cell at 197,759 /km², 1.7x the densest census ward).

Why GHSL is NOT rescaled to the census here: a dry run on Bengaluru gave ward factors of 0.12–53 — the city's growth since
2011 is in the outer wards (2011 shares would shrink them ~7x and inflate the old core), and small core wards hold only 2–3
cells, so their GHSL totals are too noisy to scale against. So both layers are shown, each for what it is.

Env: RUN_TOKEN, AIRAWAT_API, TENANT, WARD_GEOJSON (file id of the ward boundary file carrying the census attributes),
     WARD_NO_KEY (WARD_NO), POP_KEY (POP_TOTAL), AREA_KEY (AREA_SQ_KM), CENSUS_YEAR (2011).
"""
import base64, json, os, re, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TENANT = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""
CID = os.environ.get("AIROS_COMPONENT_ID", "ds:airawat:engine/census-ward")
CVER = os.environ.get("AIROS_COMPONENT_VERSION", "0.1.0")
NO_KEY = os.environ.get("WARD_NO_KEY", "WARD_NO")
POP_KEY = os.environ.get("POP_KEY", "POP_TOTAL")
AREA_KEY = os.environ.get("AREA_KEY", "AREA_SQ_KM")
YEAR = os.environ.get("CENSUS_YEAR", "2011")
BATCH, PAGE = 2000, 20000


def ward_no(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return str(int(v))
    m = re.search(r"(\d{1,3})", str(v))
    return str(int(m.group(1))) if m else None


def census_cells(ward_of, census, centroid, now):
    rows = []
    for h, w in ward_of.items():
        c = census.get(w)
        if not c:
            continue
        r = {"schema": "demographics.census_ward", "id": "ds:%s:demographics/census_ward/%s" % (TENANT, h), "tenant": TENANT,
             "cell_ref": {"h3_id": h, **({"centroid": centroid[h]} if centroid.get(h) else {})},
             "ward_no": w, "ward_name": c.get("name"), "census_year": YEAR, "census_population": c["pop"],
             "area_km2": c.get("area_km2"),
             "census_density_per_km2": round(c["pop"] / c["area_km2"]) if c.get("area_km2") else None,
             "observed_at": "%s-03-01T00:00:00Z" % YEAR,
             "prov": {"component": "%s@%s" % (CID, CVER), "method": "census %s ward totals (the ward boundary file attributes)" % YEAR,
                      "observed_at": now}}
        rows.append({k: v for k, v in r.items() if v is not None})
    return rows


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Run-Token": os.environ["RUN_TOKEN"]})
    return json.loads(urllib.request.urlopen(req, timeout=180).read() or b"{}")


def read_all(schema):
    out, seen, cursor = [], set(), None
    for _ in range(20):
        body = {"scope": schema, "limit": PAGE}
        if cursor:
            body["cursor"] = cursor
        r = call("/v1/query", body)
        rows = [(x.get("object") or x) for x in (r.get("results") or [])]
        new = [o for o in rows if o.get("id") not in seen]
        seen.update(o.get("id") for o in new); out += new
        cursor = r.get("next_cursor")
        if not cursor or not new or len(rows) < PAGE:
            break
    return [o for o in out if str(o.get("id", "")).startswith("ds:%s:" % TENANT)]


def _write(rows, now):
    written = failed = 0
    for i in range(0, len(rows), BATCH):
        items = [{"scope": r["schema"], "id": r["id"], "object": r, "valid_time": {"from": r.get("observed_at") or now}} for r in rows[i:i + BATCH]]
        res = call("/v1/write/batch", {"items": items, "asserted_at": now})
        written += int(res.get("written", 0)); failed += int(res.get("error_count", len(res.get("errors") or [])))
        for e in (res.get("errors") or [])[:2]:
            print("  ⚠ write refused (%s): %s" % (e.get("id"), str(e.get("error"))[:240]), flush=True)
    return written, failed


def main():
    if not TENANT:
        print("FAIL: TENANT not set — refusing to guess."); return 1
    fid = os.environ.get("WARD_GEOJSON", "").strip()
    if not fid:
        print("FAIL: WARD_GEOJSON not set — the ward boundary file carrying census attributes."); return 1
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    gj = json.loads(base64.b64decode(call("/v1/file/get", {"id": fid}).get("content_b64") or b"{}") or b"{}")
    census = {}
    for f in gj.get("features") or []:
        p = f.get("properties") or {}
        w = ward_no(p.get(NO_KEY))
        if w and isinstance(p.get(POP_KEY), (int, float)) and p[POP_KEY] > 0:
            census[w] = {"pop": float(p[POP_KEY]), "area_km2": p.get(AREA_KEY), "name": p.get("WARD_NAME") or p.get("name")}
    ward_of = {c["h3_id"]: ward_no(c.get("ward_no")) for c in read_all("space.ward_cell") if c.get("h3_id")}
    centroid = {(r.get("cell_ref") or {}).get("h3_id"): (r.get("cell_ref") or {}).get("centroid") for r in read_all("city.cell_profile")}
    print("census wards %d · ward cells %d" % (len(census), len(ward_of)), flush=True)
    if not (census and ward_of):
        print("FAIL: need the census attributes and space.ward_cell (run ward-h3-map)."); return 1
    rows = census_cells(ward_of, census, centroid, now)
    w, f = _write(rows, now)
    dens = sorted(r["census_density_per_km2"] for r in rows if r.get("census_density_per_km2"))
    print("RESULT: " + json.dumps({"census_cells": len(rows), "written": w, "failed": f, "wards": len({r["ward_no"] for r in rows}),
                                   "census_density_max": dens[-1] if dens else None}))
    return 0 if not f else 1


if __name__ == "__main__":
    raise SystemExit(main())
