"""Collection Capture Engine — the field-capture seam behind the Waste Collector App.

waste-route          : given a ward, list the properties on that route (buildings inside the ward polygon) with
                       each one's latest pickup status, so the collector knows what's left to do.
waste-record-pickup  : record ONE property's pickup (collected | missed) as a governed waste.pickup event,
                       stamped with the collector's position (or the building centroid), attributed to this engine.

This is how REAL collection data enters the platform (replacing the illustrative seed) — the coverage engine and
the Waste Console read whatever pickups exist, so a recorded visit immediately moves the numbers.
"""
import time as _t

CID = "ds:airawatdeveloper:engine/collection-capture-engine"
def _now(): return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
def _today(): return _t.strftime("%Y-%m-%d", _t.gmtime())

def _ll(c):
    """Normalise a centroid/space to (lon, lat) from {lon,lat} | {x,y} | [lon,lat]."""
    if isinstance(c, dict):
        lon = c.get("lon", c.get("lng", c.get("x"))); lat = c.get("lat", c.get("y"))
        return (lon, lat)
    if isinstance(c, (list, tuple)) and len(c) >= 2:
        return (c[0], c[1])
    return (None, None)

def _in_ring(lon, lat, ring):
    inside = False; n = len(ring); j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]; xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside

def _ward(ctx, ward_id):
    for w in ctx["kfind"]("geo.ward"):
        if w["id"] == ward_id or w["id"].split("/")[-1] == str(ward_id):
            b = w.get("boundary") or {}; coords = b.get("coordinates")
            if not coords: return None
            ring = coords[0] if b.get("type") == "Polygon" else (coords[0][0] if coords and coords[0] else None)
            if not ring or len(ring) < 3: return None
            xs = [c[0] for c in ring]; ys = [c[1] for c in ring]
            return {"id": w["id"], "name": w.get("name"), "ring": ring, "bbox": (min(xs), min(ys), max(xs), max(ys))}
    return None

def _pid(tenant, building_id):
    return f"ds:{tenant}:waste/pickup/{building_id.split('/')[-1]}"

def h_route(ctx):
    body = ctx.get("body") or {}; ward_id = body.get("ward_id")
    cap = int(body.get("limit", 400))
    if not ward_id:
        return {"error": "ward_id required"}, None
    w = _ward(ctx, ward_id)
    if not w:
        return {"error": f"ward '{ward_id}' not found or has no boundary"}, None
    x0, y0, x1, y1 = w["bbox"]
    # latest pickup status per building (by subject), today's captures flagged
    status = {}
    for p in ctx["kfind"]("waste.pickup"):
        subj = p.get("subject")
        if subj:
            status[subj] = {"status": p.get("status"), "at": p.get("at", ""), "today": str(p.get("at", "")).startswith(_today())}
    props = []
    for b in ctx["kfind"]("geo.building"):
        lon, lat = _ll(b.get("centroid") or {})
        if lon is None or lat is None:
            continue
        if not (x0 <= lon <= x1 and y0 <= lat <= y1) or not _in_ring(lon, lat, w["ring"]):
            continue
        st = status.get(b["id"]) or {}
        props.append({"id": b["id"], "lon": round(lon, 6), "lat": round(lat, 6),
                      "status": st.get("status"), "today": st.get("today", False)})
        if len(props) >= cap:
            break
    done = sum(1 for p in props if p["today"])
    # ward outline (rounded) so the app can draw the route in context — no external map tiles under the sandbox CSP
    ring = [[round(c[0], 6), round(c[1], 6)] for c in w["ring"]]
    return {"ward_id": w["id"], "ward": w["name"], "total": len(props), "done_today": done,
            "properties": props, "boundary": ring, "capped": len(props) >= cap}, None

def h_record(ctx):
    tenant = ctx["tenant"]; body = ctx.get("body") or {}
    bid = body.get("building_id"); st = body.get("status")
    if not bid or st not in ("completed", "missed"):
        return {"error": "building_id + status(completed|missed) required"}, None
    lon, lat = body.get("lon"), body.get("lat")
    pid = _pid(tenant, bid)
    rec = {"schema": "waste.pickup", "id": pid, "tenant": tenant, "at": _now(),
           "subject": bid, "subject_kind": "premise", "status": st, "provider": "waste-collector-app",
           "prov": {"method": "Field capture via Waste Collector App — real collector check-in",
                    "actor": ctx.get("sub")}}
    if lon is not None and lat is not None:
        rec["space"] = {"lon": round(float(lon), 6), "lat": round(float(lat), 6)}
    ctx["kwrite"](pid, rec)
    return {"saved": pid, "building": bid, "status": st, "at": rec["at"]}, None

HANDLERS = {"waste-route": h_route, "waste-record-pickup": h_record}

def _selftest():
    ward = {"id": "ds:x:geo/ward/w1", "name": "Ward 1",
            "boundary": {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]}}
    blds = [{"id": "ds:x:building/h/b1", "centroid": {"lon": 0.5, "lat": 0.5}},
            {"id": "ds:x:building/h/b2", "centroid": [1.5, 1.5]},
            {"id": "ds:x:building/h/out", "centroid": {"lon": 9, "lat": 9}}]  # outside → excluded
    store = {}
    base = {"tenant": "x", "sub": "collector1",
            "kwrite": lambda i, o: store.__setitem__(i, o),
            "kfind": lambda s: {"geo.ward": [ward], "geo.building": blds, "waste.pickup": []}.get(s, [])}
    r, _ = h_route(dict(base, body={"ward_id": "w1"}))
    assert r["total"] == 2 and r["done_today"] == 0, r
    rec, _ = h_record(dict(base, body={"building_id": "ds:x:building/h/b1", "status": "completed", "lon": 0.5, "lat": 0.5}))
    assert rec["saved"] == "ds:x:waste/pickup/b1" and store["ds:x:waste/pickup/b1"]["status"] == "completed", rec
    assert store["ds:x:waste/pickup/b1"]["subject_kind"] == "premise", store
    print("selftest OK — route:", {k: r[k] for k in ("ward", "total", "done_today")}, "| record:", rec["saved"])

def main():
    print("Collection Capture Engine — handlers:", ", ".join(sorted(HANDLERS))); _selftest(); return 0

if __name__ == "__main__":
    raise SystemExit(main())
