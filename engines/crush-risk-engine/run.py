#!/usr/bin/env python3
"""Crush-Risk Engine — a confined engine. Reads crowd-density facts, applies the governed density-band
rule, and writes derived `cell.crush_risk` facts (L3). This is the DERIVATION step split out of the crowd
sensor: the sensor writes cell.crowd_density; this engine turns density into a graded crush-risk band per
the `crowd-crush-density-bands` rule (>=4.5/m² DANGER, >=6/m² CRITICAL). All I/O is through the broker.
"""
import json, os, urllib.request

API   = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ.get("RUN_TOKEN", "")   # lazy: module must import for in-process HANDLERS/lifecycle dispatch

def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())

def context():
    req = urllib.request.Request(API + "/v1/context", headers={"X-Run-Token": TOKEN})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

def band(d, danger, critical):
    return "critical" if d >= critical else "danger" if d >= danger else None

def main():
    T = context().get("tenant") or os.environ.get("WRITE_TENANT", "kmc")
    # thresholds come from the governed rule (fall back to the documented bands if it isn't seeded yet)
    danger, critical = 4.5, 6.0
    for r in (call("/v1/query", {"scope": "governance.rule", "limit": 500}).get("results") or []):
        o = r.get("object") or r
        if o.get("rule_id") == "crowd-crush-density-bands" and isinstance(o.get("value"), list) and len(o["value"]) >= 2:
            danger, critical = float(o["value"][0]), float(o["value"][1]); break

    rows = [r.get("object") or r for r in call("/v1/query", {"scope": "cell.crowd_density", "limit": 50000}).get("results", [])]
    written = 0
    for c in rows:
        d = c.get("density_sqm")
        if d is None:
            continue
        b = band(d, danger, critical)
        if not b:
            continue                                        # below the danger threshold → no crush-risk fact
        h3id = (c.get("space") or {}).get("h3_id") or c["id"].split("/")[-1]
        cid = f"ds:{T}:cell/crush_risk/{c['id'].split('/')[-1]}"
        obj = {"schema": "cell.crush_risk", "id": cid, "tenant": T, "layer": "L3",
               "space": c.get("space"), "observed_at": c.get("observed_at"), "density_sqm": d,
               "band": b, "threshold": danger, "site": c.get("site"),
               "method": "density-threshold", "derived_from": c["id"], "prov": c.get("prov")}
        call("/v1/write", {"scope": "cell.crush_risk", "id": cid, "object": obj})
        written += 1
    print(f"crush-risk-engine: read {len(rows)} density cells; wrote {written} cell.crush_risk "
          f"(danger>={danger}, critical>={critical}) to {T}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())


# ── ADR-0104 lifecycle hook: on_install ──────────────────────────────────────────────────────────
# When Crowd Intelligence is installed into a tenant, seed the crowd viz.layer DESCRIPTORS into that
# tenant's registry so the Crowd layers appear automatically in City Scan / City Intelligence (whose
# layer catalogue is registry-driven). Runs in the confined engine ctx (ctx["kwrite"], attributed to
# this component under its viz.layer write grant). Idempotent — re-install just re-writes the same ids.
_CROWD_LAYERS = [
    {"layer_id": "crowd-density", "label": "Crowd Density", "short_label": "Crowd", "order": 10,
     "category": "people", "color_scale": "reds", "value_field": "density_sqm", "unit": "/m²",
     "source_schema": "cell.crowd_density",
     "interpretation": "People per m² per cell. >4.5/m² = crush danger; >6 = critical."},
    {"layer_id": "crush-risk", "label": "Crush Risk", "short_label": "Crush", "order": 11,
     "category": "people", "color_scale": "reds", "value_field": "density_sqm", "unit": "/m²",
     "source_schema": "cell.crush_risk",
     "interpretation": "Cells past the crush-danger density threshold (derived from crowd density)."},
    {"layer_id": "ai-crowd-count", "label": "AI Crowd Count", "short_label": "CCTV", "order": 12,
     "category": "people", "color_scale": "reds", "value_field": "count",
     "source_schema": "vision.detection",
     "interpretation": "People counted per cell by CCTV person-detection."},
]


def h_seed_layers(ctx):
    """on_install: register the Crowd Intelligence viz.layer descriptors into the installing tenant."""
    tenant = ctx.get("tenant")
    if not tenant:
        return None, "no tenant in install context"
    n = 0
    for L in _CROWD_LAYERS:
        vid = f"ds:{tenant}:viz/layer/{L['layer_id']}"
        ctx["kwrite"](vid, {"schema": "viz.layer", "id": vid, "enabled": True, "render": "map", **L})
        n += 1
    return {"seeded_layers": n, "tenant": tenant,
            "layers": [L["layer_id"] for L in _CROWD_LAYERS]}, None


HANDLERS = {"seed-layers": h_seed_layers, "on_install": h_seed_layers}
