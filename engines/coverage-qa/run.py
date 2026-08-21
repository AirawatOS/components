#!/usr/bin/env python3
"""coverage-qa: the TRUST-LAYER engine (ADR-0060 §5).

Confined — its only I/O is the kernel broker. For an AOI it assesses each data LAYER (a governed noun) and
writes one city.coverage record so a commissioner sees, per layer: how much of the AOI it covers, how fresh
it is, where it came from, how confident it is, what's missing — and the headline AUTHORITY class:

  authoritative  a real official / municipal dataset
  open-data      real but third-party (OSM, Open-Meteo, ESA WorldCover…)
  ai-derived     a platform estimate — every cell.* indicator, or anything produced in synthetic mode

Authority is INFERRED from the facts themselves (never asserted): a derived cell measure (produced by an
engine, carries a `method`) is ai-derived; synthetic-mode acquisition is ai-derived; otherwise it is
open-data if its source is a known open catalogue, else authoritative. The output is only as honest as its
inputs — that is the point.

Env: AOI_ID · TENANT · WORKSPACE (city.workspace id) · LAYERS (comma-sep nouns to assess)
"""
import json, os, math, urllib.request
import h3

AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/kanpur-core")
TENANT = os.environ.get("TENANT", "kmc")
WORKSPACE = os.environ.get("WORKSPACE", f"ds:{TENANT}:city/workspace/{AOI_ID.rsplit('/', 1)[-1]}")
LAYERS = [s.strip() for s in os.environ.get(
    "LAYERS", "geo.landcover,environment.lst_observation,cell.builtup,cell.greencover,cell.heat").split(",") if s.strip()]
ASSESSED_AT = os.environ.get("ASSESSED_AT", "2025-06-01T00:00:00Z")
CID = os.environ.get("AIROS_COMPONENT_ID", "ds:airawat:engine/coverage-qa")

# ref.source ids considered OPEN third-party data (real, not municipal-authoritative, not AI)
OPEN_SOURCES = {"ds:airawat:ref/source/osm", "ds:airawat:ref/source/open-meteo",
                "ds:airawat:ref/source/esa-worldcover", "ds:airawat:ref/source/openaq",
                "ds:airawat:ref/source/cpcb"}


def _call(path, body):
    req = urllib.request.Request(os.environ.get("AIRAWAT_API", "http://localhost:8080") + path,
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
        "X-Run-Token": os.environ["RUN_TOKEN"]}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def cells_in_bbox(bbox, res):
    w, s, e, n = bbox
    try:
        return set(h3.polygon_to_cells(h3.LatLngPoly([(s, w), (s, e), (n, e), (n, w)]), res))
    except Exception:
        cells = set(); step = h3.average_hexagon_edge_length(res, unit="km") / 111.0 / 2.0
        lat = s
        while lat <= n:
            lng = w
            while lng <= e:
                cells.add(h3.latlng_to_cell(lat, lng, res)); lng += step
            lat += step
        return cells


def _covered_cells(obj, res):
    """Which res-cells this fact occupies: a cell measure → its own cell; a point → its cell; a polygon →
    its polyfill (parents). Returns a set (∩ AOI done by the caller)."""
    cr = obj.get("cell_ref") or {}
    if cr.get("h3_id"):
        return {cr["h3_id"]} if cr.get("resolution", res) == res else {h3.cell_to_parent(cr["h3_id"], res)}
    if obj.get("latitude") is not None and obj.get("longitude") is not None:
        return {h3.latlng_to_cell(obj["latitude"], obj["longitude"], res)}
    g = obj.get("geometry")
    if isinstance(g, dict) and g.get("type") == "Polygon" and g.get("coordinates"):
        try:
            ring = [(c[1], c[0]) for c in g["coordinates"][0]]
            fine = h3.polygon_to_cells(h3.LatLngPoly(ring), min(res + 2, 15))
            return {h3.cell_to_parent(fc, res) for fc in fine}
        except Exception:
            return set()
    return set()


def _authority(objs):
    """Infer the trust class from the facts (never asserted)."""
    syn = any(str(o.get("acquired_via", "")).endswith(":synthetic")
              or str(o.get("satellite_name", "")).startswith("synthetic") for o in objs)
    derived = any(o.get("method") or (o.get("produced_by", {}).get("component", {}).get("id", "")).find("engine/") >= 0
                  or (o.get("layer") == "L2") for o in objs)
    srcs = {o.get("source_ref") or o.get("source") for o in objs if (o.get("source_ref") or o.get("source"))}
    if syn:
        return "ai-derived", srcs, "synthetic reference data (not a real acquisition)"
    if derived:
        return "ai-derived", srcs, None
    if srcs and srcs <= OPEN_SOURCES:
        return "open-data", srcs, None
    return "authoritative", srcs, None


def main():
    aoi = _call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object")
    if not aoi:
        print(f"FAIL: no AOI {AOI_ID}"); return 1
    res = int(aoi.get("h3_res", 9)); total = len(cells_in_bbox(aoi["bbox"], res))
    print(f"coverage-qa · AOI '{aoi.get('name', AOI_ID)}' res={res} ({total} cells) · workspace {WORKSPACE}")
    print(f"{'layer':30s} {'cov%':>5s} {'conf':>5s} {'authority':13s} freshness   n   source")

    written = 0
    for noun in LAYERS:
        rows = _call("/v1/query", {"scope": noun, "limit": 20000}).get("results", [])
        objs = [r.get("object") or r for r in rows]
        covered = set()
        for o in objs:
            covered |= _covered_cells(o, res)
        covered &= cells_in_bbox(aoi["bbox"], res)
        cov_pct = round(100.0 * len(covered) / total, 1) if total else 0.0
        confs = [o["confidence"] for o in objs if isinstance(o.get("confidence"), (int, float))]
        conf = round(sum(confs) / len(confs), 2) if confs else None
        fresh = max((o.get("observed_at") for o in objs if o.get("observed_at")), default=None)
        authority, srcs, syn_gap = _authority(objs)
        source_ref = sorted(srcs)[0] if srcs else None
        gaps = []
        if syn_gap:
            gaps.append(syn_gap)
        if total and len(covered) < total:
            gaps.append(f"{total - len(covered)}/{total} AOI cells uncovered")
        if source_ref == "ds:airawat:ref/source/open-meteo":
            gaps.append("coarse source (~11km) — intra-city variation limited")
        if not objs:
            gaps.append("no data")

        rec = {"schema": "city.coverage", "id": f"ds:{TENANT}:city/coverage/{WORKSPACE.rsplit('/', 1)[-1]}--{noun.replace('.', '_')}",
               "workspace": WORKSPACE, "layer_noun": noun, "coverage_pct": cov_pct,
               "freshness": (fresh or "")[:10] or None, "source_ref": source_ref, "confidence": conf,
               "authority": authority, "gaps": gaps,
               "assessed_by": {"component": {"id": CID, "version": "0.1.0"}}, "assessed_at": ASSESSED_AT}
        _call("/v1/write", {"scope": "city.coverage", "id": rec["id"], "object": rec,
                            "valid_time": {"from": ASSESSED_AT}, "asserted_at": ASSESSED_AT})
        written += 1
        print(f"{noun:30s} {cov_pct:5.1f} {('  -' if conf is None else f'{conf:5.2f}')} "
              f"{authority:13s} {(rec['freshness'] or '    -   ')}  {len(objs):3d}  {(source_ref or '-').rsplit('/', 1)[-1]}")
        if gaps:
            print(f"{'':30s}   gaps: {'; '.join(gaps)}")

    print(f"\nwrote {written} city.coverage records — the baseline's trust report for {WORKSPACE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
