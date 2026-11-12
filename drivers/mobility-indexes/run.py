#!/usr/bin/env python3
"""Mobility Indexes — three per-cell mobility layers from the tenant's OpenStreetMap extract (+ population).

  mobility.walkability     walkability_score (0–100): junction density, street density and (inverted) dead-end share,
                           each a percentile within the city, averaged. Walkable = every highway class but motorways/trunks.
  mobility.transit_access  transit_gap_score (0–100): people living beyond a walkable bus stop (> 500 m) — and beyond a
                           metro station (> 1 km) — weighted by population; plus the distances themselves.
  mobility.chokepoint      chokepoint_score (0–100): how many shortest travel-time routes on the ARTERIAL network pass
                           through the cell (sampled edge betweenness, Brandes), as a percentile. A PROXY for where
                           congestion concentrates — not observed traffic.

Reads the OSM PBF (OSM_PBF_FILE_ID — the same extract City Setup fetched) with osmium: true node topology (junctions =
nodes shared by 3+ street segments; dead ends = degree 1), clipped to the AOI. Cells and population come from
demographics.population. Distances are straight-line (haversine), not network distance — stated in each record.
No synthetic data: a cell with no streets gets no walkability record; no stops at all → no transit records.

Env: RUN_TOKEN, AIRAWAT_API, TENANT/WRITE_TENANT, OSM_PBF_FILE_ID, AOI_ID (opt), CHOKE_SAMPLES (default 150),
     BUS_WALK_M (500), METRO_WALK_M (1000).
"""
import base64, heapq, json, math, os, random, shutil, subprocess, tempfile, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TENANT = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""
AOI_HINT = os.environ.get("AOI_ID", "")
CID = os.environ.get("AIROS_COMPONENT_ID", "ds:airawat:driver/mobility-indexes")
CVER = os.environ.get("AIROS_COMPONENT_VERSION", "0.1.0")
SAMPLES = int(os.environ.get("CHOKE_SAMPLES", "150"))
BUS_M = float(os.environ.get("BUS_WALK_M", "500"))
METRO_M = float(os.environ.get("METRO_WALK_M", "1000"))
BATCH, PAGE = 2000, 20000

NOT_WALKABLE = {"motorway", "motorway_link", "trunk", "trunk_link", "construction", "proposed", "raceway", "bus_guideway"}
ARTERIAL_KMH = {"motorway": 60, "motorway_link": 40, "trunk": 50, "trunk_link": 35, "primary": 40, "primary_link": 30,
                "secondary": 35, "secondary_link": 25, "tertiary": 30, "tertiary_link": 25}


def hav(lon1, lat1, lon2, lat2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742000 * math.asin(math.sqrt(min(1.0, a)))


def pct_rank(values):
    """value → percentile 0..100 = the share of the city's cells at or below it (the top cell scores 100)."""
    v = sorted(values)
    n = len(v)
    if not n:
        return lambda x: None
    import bisect
    return lambda x: round(100.0 * bisect.bisect_right(v, x) / n, 1)


# ── network → per-cell street measures ─────────────────────────────────────────────────────────────────────
def street_measures(ways, cell_of):
    """ways: [(highway_class, [(node_id, lon, lat), ...]), ...] → {h3: {street_m, junctions, dead_ends, nodes}}.
    A node's degree = the number of walkable street segments touching it (across ways)."""
    deg, where = {}, {}
    per = {}
    for cls, nodes in ways:
        if cls in NOT_WALKABLE or len(nodes) < 2:
            continue
        for (a, ax, ay), (b, bx, by) in zip(nodes, nodes[1:]):
            deg[a] = deg.get(a, 0) + 1; deg[b] = deg.get(b, 0) + 1
            where[a] = (ax, ay); where[b] = (bx, by)
            h = cell_of((ax + bx) / 2, (ay + by) / 2)
            if h:
                per.setdefault(h, {"street_m": 0.0, "junctions": 0, "dead_ends": 0, "nodes": 0})["street_m"] += hav(ax, ay, bx, by)
    for n, d in deg.items():
        h = cell_of(*where[n])
        if not h:
            continue
        c = per.setdefault(h, {"street_m": 0.0, "junctions": 0, "dead_ends": 0, "nodes": 0})
        if d >= 3:
            c["junctions"] += 1; c["nodes"] += 1
        elif d == 1:
            c["dead_ends"] += 1; c["nodes"] += 1
    return per


def walkability(per, area_km2):
    """per-cell measures → {h3: record fields} with walkability_score = mean of three city percentiles."""
    rows = {h: {"street_density_km_per_km2": round(c["street_m"] / 1000 / area_km2(h), 2),
                "intersection_density_per_km2": round(c["junctions"] / area_km2(h), 1),
                "dead_end_share": round(c["dead_ends"] / c["nodes"], 3) if c["nodes"] else 0.0}
            for h, c in per.items() if c["street_m"] > 0}
    pi = pct_rank([r["intersection_density_per_km2"] for r in rows.values()])
    ps = pct_rank([r["street_density_km_per_km2"] for r in rows.values()])
    pd = pct_rank([r["dead_end_share"] for r in rows.values()])
    for r in rows.values():
        r["walkability_score"] = round((pi(r["intersection_density_per_km2"]) + ps(r["street_density_km_per_km2"])
                                        + (100 - pd(r["dead_end_share"]))) / 3, 1)
    return rows


# ── transit access ─────────────────────────────────────────────────────────────────────────────────────────
class Grid:
    """A coarse lon/lat bucket index for nearest-point queries (~1.1 km buckets)."""
    def __init__(self, pts, step=0.01):
        self.step, self.b = step, {}
        for x, y in pts:
            self.b.setdefault((int(x // step), int(y // step)), []).append((x, y))
        self.n = len(pts)

    def nearest(self, x, y, max_rings=60):
        if not self.n:
            return None
        cx, cy = int(x // self.step), int(y // self.step)
        best = None
        for r in range(max_rings):
            for i in range(cx - r, cx + r + 1):
                for j in range(cy - r, cy + r + 1):
                    if max(abs(i - cx), abs(j - cy)) != r:
                        continue
                    for px, py in self.b.get((i, j), ()):
                        d = hav(x, y, px, py)
                        if best is None or d < best:
                            best = d
            if best is not None and best < (r * self.step) * 100000:
                break
        return best


def transit_access(cells, bus, metro):
    """cells: {h3: (lon, lat, population)} → {h3: fields}. Gap weight = how far beyond a walkable stop the cell is
    (0 within BUS_M, rising to 1 at BUS_M + 1 km), halved if a metro station is within METRO_M; × population."""
    gb, gm = Grid(bus), Grid(metro)
    rows = {}
    for h, (x, y, pop) in cells.items():
        db, dm = gb.nearest(x, y), gm.nearest(x, y)
        if db is None and dm is None:
            continue
        w = 1.0 if db is None else max(0.0, min(1.0, (db - BUS_M) / 1000.0))
        if dm is not None and dm <= METRO_M:
            w *= 0.5
        rows[h] = {"dist_bus_stop_m": None if db is None else round(db), "dist_metro_m": None if dm is None else round(dm),
                   "population": pop, "_need": w * (pop or 0)}
    pr = pct_rank([r["_need"] for r in rows.values() if r["_need"] > 0])
    for r in rows.values():
        r["transit_gap_score"] = pr(r["_need"]) if r["_need"] > 0 else 0.0
        r["underserved"] = bool(r["_need"] > 0)
        del r["_need"]
    return rows


# ── arterial chokepoints (sampled edge betweenness on the contracted travel-time graph) ───────────────────────
def arterial_graph(ways):
    """→ (adj {node: [(nbr, seconds, edge_id)]}, edges {edge_id: [(lon, lat) midpoints]}). Degree-2 chains between
    junctions / way ends are contracted into one edge, so the graph stays small."""
    use = {}
    for cls, nodes in ways:
        if cls in ARTERIAL_KMH:
            for k, (n, _, _) in enumerate(nodes):
                use[n] = use.get(n, 0) + (1 if 0 < k < len(nodes) - 1 else 2)
    adj, edges, eid = {}, {}, 0
    for cls, nodes in ways:
        if cls not in ARTERIAL_KMH or len(nodes) < 2:
            continue
        mps = 1000.0 * ARTERIAL_KMH[cls] / 3600.0
        start, secs, mids = nodes[0], 0.0, []
        for (a, ax, ay), (b, bx, by) in zip(nodes, nodes[1:]):
            secs += hav(ax, ay, bx, by) / mps
            mids.append(((ax + bx) / 2, (ay + by) / 2))
            if use.get(b, 0) >= 2 or b == nodes[-1][0]:          # a junction or the way's end → close the edge
                eid += 1
                edges[eid] = mids
                adj.setdefault(start[0], []).append((b, secs, eid))
                adj.setdefault(b, []).append((start[0], secs, eid))   # two-way (one-way tags ignored: a proxy)
                start, secs, mids = (b, bx, by), 0.0, []
    return adj, edges


def edge_betweenness(adj, samples, seed=7):
    """Brandes (weighted) from `samples` random sources → {edge_id: score}."""
    nodes = list(adj)
    if not nodes:
        return {}
    rnd = random.Random(seed)
    srcs = nodes if len(nodes) <= samples else rnd.sample(nodes, samples)
    eb = {}
    for s in srcs:
        dist, sigma, preds, order = {s: 0.0}, {s: 1.0}, {s: []}, []
        pq, seen = [(0.0, s)], set()
        while pq:
            d, v = heapq.heappop(pq)
            if v in seen:
                continue
            seen.add(v); order.append(v)
            for w, wt, e in adj.get(v, ()):
                nd = d + wt
                if w not in dist or nd < dist[w] - 1e-9:
                    dist[w] = nd; sigma[w] = sigma[v]; preds[w] = [(v, e)]
                    heapq.heappush(pq, (nd, w))
                elif abs(nd - dist[w]) <= 1e-9:
                    sigma[w] += sigma[v]; preds[w].append((v, e))
        delta = {v: 0.0 for v in order}
        for w in reversed(order):
            for v, e in preds.get(w, ()):
                c = sigma[v] / sigma[w] * (1.0 + delta[w])
                eb[e] = eb.get(e, 0.0) + c
                delta[v] += c
    return eb


def chokepoints(adj, edges, cell_of, samples):
    eb = edge_betweenness(adj, samples)
    per = {}
    for e, mids in edges.items():
        b = eb.get(e, 0.0)
        for x, y in mids:
            h = cell_of(x, y)
            if h and b > per.get(h, -1):
                per[h] = b
    top = max(per.values()) if per else 0.0
    pr = pct_rank([v for v in per.values() if v > 0])
    return {h: {"betweenness": round(v / top, 4) if top else 0.0, "chokepoint_score": pr(v) if v > 0 else 0.0}
            for h, v in per.items()}


# ── I/O ────────────────────────────────────────────────────────────────────────────────────────────────────
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
    return out


def resolve_aoi():
    rows = [(r.get("object") or r) for r in (call("/v1/query", {"scope": "space.aoi"}).get("results") or [])]
    mine = [o for o in rows if len(o.get("bbox") or []) == 4 and (not AOI_HINT or o.get("id") == AOI_HINT)]
    if not mine:
        raise SystemExit("FATAL: no space.aoi for this tenant — run City Setup first.")
    return [float(v) for v in max(mine, key=lambda a: abs((a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])))["bbox"]]


def pbf_from_asset(fid):
    raw = base64.b64decode(call("/v1/file/get", {"id": fid}).get("content_b64") or "")
    if not raw:
        raise SystemExit("FATAL: OSM PBF %s is empty / not in the filestore" % fid)
    tf = tempfile.NamedTemporaryFile(suffix=".osm.pbf", delete=False); tf.write(raw); tf.close()
    print("  read OSM PBF %s… (%d MB)" % (fid[:20], len(raw) // 1048576), flush=True)
    return tf.name


def clip(src, bbox):
    if not shutil.which("osmium"):
        return src
    w, s, e, n = bbox; pad = 0.02; dst = src + ".clip.osm.pbf"
    subprocess.run(["osmium", "extract", "-b", "%s,%s,%s,%s" % (w - pad, s - pad, e + pad, n + pad), src, "-o", dst,
                    "--overwrite", "-s", "complete_ways"], check=True, capture_output=True, timeout=600)
    print("  clipped to the AOI → %d MB" % (os.path.getsize(dst) // 1048576), flush=True)
    return dst


def parse(pbf, bbox):
    import osmium
    w, s, e, n = bbox
    inside = lambda x, y: w <= x <= e and s <= y <= n
    ways, bus, metro = [], [], []

    class H(osmium.SimpleHandler):
        def node(self, nd):
            t = nd.tags
            if not nd.location.valid():
                return
            x, y = nd.location.lon, nd.location.lat
            if not inside(x, y):
                return
            if t.get("highway") == "bus_stop" or (t.get("public_transport") == "platform" and t.get("bus") == "yes"):
                bus.append((x, y))
            if t.get("station") == "subway" or (t.get("railway") == "station" and (t.get("subway") == "yes" or
                    "metro" in (t.get("network", "") + t.get("name", "") + t.get("operator", "")).lower() or
                    "bmrcl" in t.get("operator", "").lower())):
                metro.append((x, y))

        def way(self, wy):
            cls = wy.tags.get("highway")
            if not cls:
                return
            pts = []
            for nd in wy.nodes:
                if nd.location.valid():
                    pts.append((nd.ref, nd.location.lon, nd.location.lat))
            if len(pts) >= 2 and any(inside(x, y) for _, x, y in pts):
                ways.append((cls, pts))
    H().apply_file(pbf, locations=True)
    return ways, bus, metro


def _write(rows, now):
    written = failed = 0
    for i in range(0, len(rows), BATCH):
        items = [{"scope": r["schema"], "id": r["id"], "object": r, "valid_time": {"from": now}} for r in rows[i:i + BATCH]]
        res = call("/v1/write/batch", {"items": items, "asserted_at": now})
        written += int(res.get("written", 0)); failed += int(res.get("error_count", len(res.get("errors") or [])))
        for e in (res.get("errors") or [])[:2]:
            print("  ⚠ write refused (%s): %s" % (e.get("id"), str(e.get("error"))[:240]), flush=True)
    return written, failed


def main():
    if not TENANT:
        print("FAIL: TENANT not set — refusing to guess."); return 1
    fid = os.environ.get("OSM_PBF_FILE_ID", "").strip()
    if not fid:
        print("FAIL: OSM_PBF_FILE_ID not set — fetch the OSM extract in City Setup first."); return 1
    import h3
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    bbox = resolve_aoi()
    cells = {}
    for o in read_all("demographics.population"):
        sp = o.get("space") or {}
        if str(o.get("id", "")).startswith("ds:%s:" % TENANT) and sp.get("h3") and sp.get("centroid"):
            cells[sp["h3"]] = (sp["centroid"][0], sp["centroid"][1], o.get("population"))
    if not cells:
        print("FAIL: no demographics.population cells — build the city baseline first."); return 1
    res = h3.get_resolution(next(iter(cells)))
    cell_of = lambda x, y: (lambda c: c if c in cells else None)(h3.latlng_to_cell(y, x, res))
    area = lambda h: h3.cell_area(h, unit="km^2")
    pbf = clip(pbf_from_asset(fid), bbox)
    t0 = time.time()
    ways, bus, metro = parse(pbf, bbox)
    print("  parsed %d highway ways, %d bus stops, %d metro stations in %.0fs" % (len(ways), len(bus), len(metro), time.time() - t0), flush=True)
    walk = walkability(street_measures(ways, cell_of), area)
    trans = transit_access(cells, bus, metro) if (bus or metro) else {}
    adj, edges = arterial_graph(ways)
    t0 = time.time()
    choke = chokepoints(adj, edges, cell_of, SAMPLES)
    print("  arterial graph: %d nodes, %d edges; betweenness from %d sources in %.0fs" % (len(adj), len(edges), min(SAMPLES, len(adj)), time.time() - t0), flush=True)
    prov = {"component": "%s@%s" % (CID, CVER), "source_ref": "ds:airawat:ref/source/osm", "derived_from": ["osm-pbf:" + fid],
            "observed_at": now}
    out = []
    for schema, rows, method in (
            ("mobility.walkability", walk, "osm street topology: junction density, street density, dead-end share (city percentiles)"),
            ("mobility.transit_access", trans, "straight-line distance to the nearest OSM bus stop / metro station × population"),
            ("mobility.chokepoint", choke, "sampled edge betweenness on the OSM arterial network (travel time; PROXY — not observed traffic)")):
        for h, f in rows.items():
            x, y, _ = cells[h]
            out.append(dict({"schema": schema, "id": "ds:%s:%s/%s" % (TENANT, schema.replace(".", "/"), h), "tenant": TENANT,
                             "cell_ref": {"h3_id": h, "centroid": [x, y]}, "observed_at": now,
                             "prov": dict(prov, method=method)}, **{k: v for k, v in f.items() if v is not None}))
    w, fl = _write(out, now)
    print("RESULT: " + json.dumps({"walkability_cells": len(walk), "transit_cells": len(trans), "chokepoint_cells": len(choke),
                                   "bus_stops": len(bus), "metro_stations": len(metro), "written": w, "failed": fl}))
    return 0 if not fl else 1


if __name__ == "__main__":
    raise SystemExit(main())
