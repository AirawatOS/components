#!/usr/bin/env python3
"""City Profile — the per-cell rollup of a city's baseline layers (writes city.cell_profile).

Reads each baseline layer of the run's tenant through the broker (its run token, its own read grants), joins them by
H3 cell, and writes ONE city.cell_profile per cell with the values it found:

  heat_c (cell.heat) · rainfall_mm (cell.rainfall, climate normal) · forecast_peak_precip_mm / forecast_total_precip_mm
  (cell.weather_forecast) · builtup_frac / green_frac (cell.builtup / cell.greencover) · flood_susceptibility /
  hand_mean_m / drainage_present (cell.hydrology) · population / density_per_km2 (demographics.population) ·
  building_coverage / mean_height_m / built_volume_m3 (cell.buildings) · road_density_km_per_km2 / road_length_m
  (cell.roads) · facilities {category: n} / facility_count (geo.facility)

Rules: a layer the tenant has not built is ABSENT from the profile (null), never estimated; every profile lists
`layers` {schema: observed_at} so freshness and gaps stay visible; only the write tenant's own cells are profiled (an id
from another tenant is ignored). Re-runs upsert (deterministic ids). Batch writes (/v1/write/batch).

Env: RUN_TOKEN, AIRAWAT_API (the broker), TENANT (or WRITE_TENANT), AOI_ID (optional).
"""
import json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TENANT = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""
AOI_HINT = os.environ.get("AOI_ID", "")
CID = os.environ.get("AIROS_COMPONENT_ID", "ds:airawat:engine/city-profile")
CVER = os.environ.get("AIROS_COMPONENT_VERSION", "0.1.0")
BATCH = 2000

# layer schema → {profile field: source field (dotted path)}; the cell id is read by _cell()
LAYERS = {
    "cell.heat":               {"heat_c": "value"},
    "cell.rainfall":           {"rainfall_mm": "rainfall_mm"},
    "cell.weather_forecast":   {"forecast_peak_precip_mm": "peak_precip_mm", "forecast_total_precip_mm": "total_precip_mm"},
    "cell.builtup":            {"builtup_frac": "value"},
    "cell.greencover":         {"green_frac": "value"},
    "cell.hydrology":          {"flood_susceptibility": "flood_susceptibility", "hand_mean_m": "hand_mean_m",
                                "drainage_present": "drainage_present"},
    "demographics.population": {"population": "population", "density_per_km2": "density_per_km2"},
    "cell.buildings":          {"building_coverage": "building_coverage", "mean_height_m": "mean_height_m",
                                "built_volume_m3": "built_volume_m3"},
    "cell.roads":              {"road_density_km_per_km2": "road_density_km_per_km2", "road_length_m": "road_length_m"},
}
FIELDS = [f for m in LAYERS.values() for f in m] + ["facility_count"]


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Run-Token": os.environ["RUN_TOKEN"]})
    return json.loads(urllib.request.urlopen(req, timeout=120).read() or b"{}")


def _get(o, path):
    for k in path.split("."):
        o = o.get(k) if isinstance(o, dict) else None
    return o


def _cell(o):
    """(h3 id, centroid) from any of the layer shapes: cell_ref{h3_id}, space{h3}, or a flat h3_id."""
    ref = o.get("cell_ref") or o.get("space") or {}
    h = ref.get("h3_id") or ref.get("h3") or o.get("h3_id") or o.get("h3")
    return h, (ref.get("centroid") or o.get("centroid"))


def _observed(o):
    return o.get("observed_at") or o.get("issued_at") or _get(o, "prov.observed_at") or _get(o, "prov.issued_at")


def _mine(o, tenant):
    return str(o.get("id") or "").startswith("ds:%s:" % tenant)


def build_profiles(read, tenant, aoi_id, now):
    """Pure join: `read(schema)` → list of records. Returns (profiles dict h3→record, per-layer counts)."""
    prof, counts = {}, {}

    def slot(h, centroid):
        p = prof.get(h)
        if p is None:
            p = prof[h] = {"schema": "city.cell_profile", "id": "ds:%s:city/cell_profile/%s" % (tenant, h),
                           "tenant": tenant, "aoi": aoi_id, "cell_ref": {"h3_id": h}, "layers": {}}
            p.update({f: None for f in FIELDS})
        if centroid and "centroid" not in p["cell_ref"]:
            p["cell_ref"]["centroid"] = centroid
        return p

    for schema, fmap in LAYERS.items():
        n = 0
        for o in read(schema):
            if not _mine(o, tenant):
                continue
            h, c = _cell(o)
            if not h:
                continue
            p = slot(h, c)
            for dst, src in fmap.items():
                v = _get(o, src)
                if v is not None:
                    p[dst] = v
            p["layers"][schema] = _observed(o)
            n += 1
        counts[schema] = n

    n = 0
    for o in read("geo.facility"):
        if not _mine(o, tenant):
            continue
        h, c = _cell(o)
        if not h:
            continue
        p = slot(h, c)
        cat = o.get("category") or "other"
        p.setdefault("facilities", {})
        p["facilities"][cat] = p["facilities"].get(cat, 0) + 1
        p["facility_count"] = (p["facility_count"] or 0) + 1
        p["layers"]["geo.facility"] = _observed(o)
        n += 1
    counts["geo.facility"] = n

    built = [s for s, k in counts.items() if k]
    for p in prof.values():
        p["layers_present"] = sorted(p["layers"])
        p["observed_at"] = now
        p["prov"] = {"method": "cell-rollup", "derived_from": ["%s@aoi:%s" % (s, aoi_id) for s in built],
                     "produced_by": {"component": {"id": CID, "version": CVER}}, "observed_at": now}
    return prof, counts


def resolve_aoi():
    if AOI_HINT:
        if AOI_HINT.split(":")[1:2] != [TENANT]:
            raise SystemExit("FATAL: AOI_ID %s is not tenant %r's — refusing to profile another tenant's area" % (AOI_HINT, TENANT))
        return AOI_HINT
    rows = [(r.get("object") or r) for r in (_call("/v1/query", {"scope": "space.aoi"}).get("results") or [])]
    mine = [o for o in rows if _mine(o, TENANT)]
    if not mine:
        raise SystemExit("FATAL: no space.aoi for tenant %r — run City Setup first" % TENANT)
    return mine[0]["id"]


def main():
    if not TENANT:
        print("FAIL: TENANT not set — a rollup writes for exactly one tenant; refusing to guess.")
        return 1
    aoi = resolve_aoi()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def read(schema):
        try:
            return [(r.get("object") or r) for r in (_call("/v1/query", {"scope": schema, "limit": 20000}).get("results") or [])]
        except Exception as e:                       # a layer this tenant cannot read / has not built → absent, said aloud
            print("  %s: not read (%s) — absent from the profile" % (schema, str(e)[:120]), flush=True)
            return []

    prof, counts = build_profiles(read, TENANT, aoi, now)
    print("tenant=%s aoi=%s · layer records: %s" % (TENANT, aoi, json.dumps(counts)), flush=True)
    items, written, failed = [], 0, 0
    for p in prof.values():
        items.append({"scope": "city.cell_profile", "id": p["id"], "object": p, "valid_time": {"from": now}})
        if len(items) >= BATCH:
            r = _call("/v1/write/batch", {"items": items, "asserted_at": now})
            written += int(r.get("written", 0)); failed += len(r.get("errors") or [])
            items = []
    if items:
        r = _call("/v1/write/batch", {"items": items, "asserted_at": now})
        written += int(r.get("written", 0)); failed += len(r.get("errors") or [])
    print("RESULT: " + json.dumps({"cells": len(prof), "written": written, "failed": failed,
                                   "layers": {s: k for s, k in counts.items() if k}}))
    return 0 if prof and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
