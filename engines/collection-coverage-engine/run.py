"""Collection Coverage Engine — was every property covered? which ward is worst?

Assigns each waste.pickup event to its ward by point-in-polygon over the REAL geo.ward boundaries, computes
collection coverage per ward (completed / total), and raises a signal.alert for each ward below the coverage
floor with the missed count. Pure geometry + counting — it reports whatever pickups exist, fabricates nothing.
"""
import time as _t

CID = "ds:airawatdeveloper:engine/collection-coverage-engine"
CVER = "0.1.0"
def _now(): return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())

def _in_ring(lon, lat, ring):
    inside = False; n = len(ring); j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]; xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside

def _wards(raw):
    W = []
    for w in raw:
        b = w.get("boundary") or {}; coords = b.get("coordinates")
        if not coords:
            continue
        ring = coords[0] if b.get("type") == "Polygon" else (coords[0][0] if coords and coords[0] else None)
        if not ring or len(ring) < 3:
            continue
        xs = [c[0] for c in ring]; ys = [c[1] for c in ring]
        W.append({"id": w["id"], "name": w.get("name"), "ring": ring,
                  "bbox": (min(xs), min(ys), max(xs), max(ys)), "total": 0, "done": 0})
    return W

def h_collection_coverage(ctx):
    tenant = ctx["tenant"]; now = _now(); body = ctx.get("body") or {}
    floor = float(body.get("coverage_floor", 0.85))
    top_k = int(body.get("top_k", 15))
    W = _wards(ctx["kfind"]("geo.ward") or [])
    if not W:
        return {"error": "no geo.ward boundaries — ingest the ward driver first"}, None
    pickups = ctx["kfind"]("waste.pickup") or []

    def find(lon, lat):
        for w in W:
            x0, y0, x1, y1 = w["bbox"]
            if x0 <= lon <= x1 and y0 <= lat <= y1 and _in_ring(lon, lat, w["ring"]):
                return w
        return None

    assigned = 0
    for p in pickups:
        sp = p.get("space") or {}
        lon, lat = sp.get("lon"), sp.get("lat")
        if lon is None or lat is None:
            continue
        w = find(lon, lat)
        if not w:
            continue
        w["total"] += 1; assigned += 1
        if p.get("status") == "completed":
            w["done"] += 1

    covered = [w for w in W if w["total"] > 0]
    for w in covered:
        w["cov"] = w["done"] / w["total"]
    low = sorted([w for w in covered if w["cov"] < floor], key=lambda w: w["cov"])

    written = 0
    for w in low[:top_k]:
        missed = w["total"] - w["done"]
        # signal.alert severity enum = low | moderate | high | severe
        sev = "severe" if w["cov"] < floor - 0.15 else ("high" if w["cov"] < floor - 0.05 else "moderate")
        aid = f"ds:{tenant}:signal/alert/waste-coverage-{w['id'].split('/')[-1]}"
        ctx["kwrite"](aid, {
            "schema": "signal.alert", "id": aid, "tenant": tenant, "kind": "waste.collection_coverage",
            "severity": sev, "status": "open", "observed_at": now, "opened_at": now,
            "title": f"{w['name']}: {round(w['cov']*100)}% collection coverage — {missed:,} of {w['total']:,} properties missed",
            "count": missed, "threshold": floor})
        written += 1

    return {"wards": len(W), "wards_with_data": len(covered), "pickups_assigned": assigned,
            "low_coverage_wards": len(low), "alerts": written,
            "worst": [{"ward": w["name"], "coverage": round(w["cov"], 3), "missed": w["total"] - w["done"]}
                      for w in low[:5]]}, None

HANDLERS = {"collection-coverage-scan": h_collection_coverage}

def _selftest():
    # a 1x1 unit-square ward; 4 pickups inside (3 completed, 1 missed) → 75% coverage < 0.85 floor → alert
    ward = {"id": "ds:x:geo/ward/w1", "name": "Ward 1",
            "boundary": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}}
    pk = [{"id": f"ds:x:waste/pickup/p{i}", "space": {"lon": 0.5, "lat": 0.5}, "status": s}
          for i, s in enumerate(["completed", "completed", "completed", "missed"])]
    pk.append({"id": "ds:x:waste/pickup/out", "space": {"lon": 9, "lat": 9}, "status": "completed"})  # outside → ignored
    store = {}
    ctx = {"tenant": "x", "body": {"coverage_floor": 0.85}, "kwrite": lambda i, o: store.__setitem__(i, o),
           "kfind": lambda s: {"geo.ward": [ward], "waste.pickup": pk}.get(s, [])}
    res, err = h_collection_coverage(ctx)
    assert err is None and res["pickups_assigned"] == 4 and res["low_coverage_wards"] == 1 and res["alerts"] == 1, res
    a = list(store.values())[0]
    assert a["schema"] == "signal.alert" and a["count"] == 1 and a["severity"] == "high", a  # 75% coverage (25% missed) → high
    print("selftest OK:", {k: res[k] for k in ("wards", "pickups_assigned", "low_coverage_wards", "alerts")})
    print("  alert:", a["title"])

def main():
    print("Collection Coverage Engine — handlers:", ", ".join(sorted(HANDLERS))); _selftest(); return 0

if __name__ == "__main__":
    raise SystemExit(main())
