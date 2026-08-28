#!/usr/bin/env python3
"""building-enrichment: people PER BUILDING from cell population + footprints (ADR-0060 analytics engine).

Confined — its only I/O is the kernel broker. It disaggregates each cell's demographics.population across the
geo.building footprints inside that cell, weighted by FLOOR AREA (area_m2 x levels):

    floor_area      = area_m2 x max(1, levels)          # levels = 1 where building height is unknown
    est_population   = cell_population x (building floor_area / cell total floor_area)

By construction the building estimates SUM BACK to each cell's population — it is a redistribution, not a new
count. Writes one geo.building_analytics per footprint, with prov.derived_from_refs pinning the exact
geo.building + demographics.population inputs (reconstructability). est_population is a deterministic ESTIMATE,
never a headcount; accuracy rides on the (partial) height coverage and assumes population tracks floor area.

TRUST: real inputs only. NO synthetic fill — a building whose cell has no population, or whose cell has zero
total floor area, gets est_population left UNSET rather than fabricated. Resolution-agnostic: each building's
population cell is computed from its centroid at the population layer's own H3 resolution, so it is correct even
if footprints and population were ingested at different resolutions.

  Env: TENANT · AOI_ID (opt; else discover the tenant's largest AOI) · STOREY_M (default 3.0)
"""
import json, os, time, urllib.request
import h3

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
AOI_HINT = os.environ.get("AOI_ID", "")
STOREY_M = float(os.environ.get("STOREY_M", "3.0"))          # informational; levels already stored on geo.building
CID = os.environ.get("AIROS_COMPONENT_ID", "ds:airawat:engine/building-enrichment")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
BUILDING_SRC = "ds:airawat:ref/source/google-open-buildings"
POP_SRC = "ds:airawat:ref/source/ghsl"


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def unwrap(res):
    return res.get("object") if isinstance(res, dict) and "object" in res else res


def mine(rows):
    """Only this tenant's rows (id-prefix guard — cross-tenant read safety)."""
    out = []
    for r in rows:
        o = r.get("object") or r
        if isinstance(o, dict) and str(o.get("id", "")).startswith(f"ds:{TENANT}:"):
            out.append(o)
    return out


def resolve_aoi():
    if AOI_HINT:
        o = unwrap(call("/v1/read", {"scope": "space.aoi", "id": AOI_HINT}))
        if not isinstance(o, dict) or len(o.get("bbox") or []) != 4:
            raise SystemExit(f"FATAL: space.aoi {AOI_HINT} missing / no 4-el bbox: {o!r}")
        return o, AOI_HINT
    rows = mine(call("/v1/query", {"scope": "space.aoi"}).get("results") or [])
    cand = [a for a in rows if len(a.get("bbox") or []) == 4]
    if not cand:
        raise SystemExit(f"FATAL: no space.aoi for tenant '{TENANT}' — run City Setup first. No default AOI.")
    o = max(cand, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))
    return o, o["id"]


def load_population():
    """h3_cell -> population, plus the population layer's resolution (from the space block, else inferred)."""
    rows = mine(call("/v1/query", {"scope": "demographics.population", "limit": 200000}).get("results") or [])
    pop, pop_res = {}, None
    for o in rows:
        sp = o.get("space") or {}
        h = sp.get("h3")
        if not h:
            continue
        p = o.get("population")
        if not isinstance(p, (int, float)):
            continue
        pop[h] = float(p)
        if pop_res is None:
            r = sp.get("res")
            try:
                pop_res = int(r) if r is not None else h3.get_resolution(h)
            except Exception:
                pop_res = h3.get_resolution(h)
    return pop, pop_res


def load_buildings():
    """List of (building_id, [lng,lat] centroid, area_m2, levels). Skips rows without a usable footprint."""
    rows = mine(call("/v1/query", {"scope": "geo.building", "limit": 500000}).get("results") or [])
    out = []
    for o in rows:
        bid = o.get("id")
        cen = o.get("centroid") or []
        area = o.get("area_m2")
        if not (bid and len(cen) == 2 and isinstance(area, (int, float)) and area > 0):
            continue
        lv = o.get("levels")
        levels = float(lv) if isinstance(lv, (int, float)) and lv > 0 else 1.0
        out.append((bid, [float(cen[0]), float(cen[1])], float(area), levels))
    return out


def analytics_id(bid):
    """ds:<tenant>:building/<h>/<hash>  ->  ds:<tenant>:geo/building_analytics/<h>/<hash> (idempotent)."""
    tail = bid.split(":", 2)[-1] if bid.count(":") >= 2 else bid
    for pre in ("geo/building/", "building/"):
        if tail.startswith(pre):
            tail = tail[len(pre):]; break
    return f"ds:{TENANT}:geo/building_analytics/{tail}"


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — an engine runs in the context of exactly one tenant. Refusing to guess.")
    aoi, aoi_ref = resolve_aoi()
    pop, pop_res = load_population()
    if not pop or pop_res is None:
        raise SystemExit(f"FATAL: no demographics.population for tenant '{TENANT}' — run the population driver first. No synthetic fill.")
    buildings = load_buildings()
    if not buildings:
        raise SystemExit(f"FATAL: no geo.building footprints for tenant '{TENANT}' — run buildings-footprints first.")

    print(f"building-enrichment · AOI {aoi_ref} · {len(buildings)} footprints · "
          f"{len(pop)} population cells (res {pop_res})", flush=True)

    # Pass 1 — assign each building to its population cell (from centroid, at the population resolution) and
    # accumulate the cell's total floor area. Resolution-agnostic: no reliance on stored h3_id matching.
    parsed = []                                              # (bid, popcell, area, floor, levels, centroid)
    cell_floor = {}
    with_levels = 0
    for bid, (lng, lat), area, levels in buildings:
        popcell = h3.latlng_to_cell(lat, lng, pop_res)
        floor = area * max(1.0, levels)
        cell_floor[popcell] = cell_floor.get(popcell, 0.0) + floor
        parsed.append((bid, popcell, area, floor, levels, [lng, lat]))
        if levels > 1.0:
            with_levels += 1

    # Pass 2 — split cell population by floor-area share; write one analytics row per building.
    written = no_pop = 0
    pop_assigned = 0.0
    for bid, popcell, area, floor, levels, cen in parsed:
        cpop = pop.get(popcell)
        cfloor = cell_floor.get(popcell, 0.0)
        est = round(cpop * (floor / cfloor), 2) if (cpop and cfloor > 0) else None
        if est is None:
            no_pop += 1
        else:
            pop_assigned += est
        rec = {
            "schema": "geo.building_analytics", "id": analytics_id(bid),
            "building_ref": bid, "h3_id": popcell, "resolution": pop_res,
            "centroid": [round(cen[0], 6), round(cen[1], 6)],
            "floor_area_m2": round(floor, 1), "levels_used": levels,
            "est_population": est,
            "method": {"engine": CID, "inputs": ["geo.building", "demographics.population"],
                       "weight": "floor_area = area_m2 x max(1, levels)", "storey_m": STOREY_M},
            "source_ref": BUILDING_SRC, "confidence": 0.6,
            "prov": {"method": "floor-area-population-disaggregation",
                     "derived_from_refs": [bid, POP_SRC], "observed_at": NOW},
            "observed_at": NOW}
        rec = {k: v for k, v in rec.items() if v is not None}   # omit unset optionals (house rule)
        call("/v1/write", {"scope": "geo.building_analytics", "id": rec["id"], "object": rec,
                           "valid_time": {"from": NOW}, "asserted_at": NOW})
        written += 1
        if written % 5000 == 0:
            print(f"  …{written}/{len(parsed)} buildings (est so far {pop_assigned:,.0f} people)", flush=True)

    if not written:
        raise SystemExit("FATAL: nothing written — no buildings could be enriched (no synthetic fill).")
    print(f"wrote {written} geo.building_analytics rows · {with_levels} had real height (>1 storey) · "
          f"{no_pop} in cells with no population (est left unset) · ~{pop_assigned:,.0f} people distributed",
          flush=True)
    print(f"RESULT: {json.dumps({'buildings': written, 'with_height': with_levels, 'no_population': no_pop, 'population_distributed': round(pop_assigned)})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
