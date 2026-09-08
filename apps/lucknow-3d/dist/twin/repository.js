// twin/repository.js — the semantic city (entities + relationships), INDEPENDENT of Cesium (Rule #24).
// Phase 1 = DemoRepository over deterministic /data JSON + buildings registered by the renderer. It implements
// the same interface a future RegistryRepository will (reading governed schemas via /bridge/query) — so the
// layers/UI never change when we swap the backing store.
//
// Interface (all sync after load()):
//   load()                      → Promise, hydrate from data
//   get(id)                     → entity | null
//   getRoot()                   → the city entity
//   getParent(id)               → entity | null
//   getChildren(id)             → entity[]
//   getRelationships(id)        → {sourceId,relationshipType,targetId}[]  (both directions)
//   getDependencies(id)         → entity[]  (things this entity relies on: targets of its outgoing edges)
//   getDependents(id)           → entity[]  (things relying on this entity: sources of incoming edges)
//   ancestors(id)               → id[]  root→…→id  (for breadcrumb)
//   byLevel(type)               → entity[]

const inBbox = (lon, lat, b) => lon >= b[0] && lon <= b[2] && lat >= b[1] && lat <= b[3];

export function createDemoRepository(base) {
  base = base || "./data";
  const E = new Map();            // id → entity
  const REL = [];                 // {sourceId, relationshipType, targetId}
  const childrenIdx = new Map();  // parentId → id[]

  const index = () => {
    childrenIdx.clear();
    for (const e of E.values()) {
      if (!e.parentId) continue;
      (childrenIdx.get(e.parentId) || childrenIdx.set(e.parentId, []).get(e.parentId)).push(e.id);
    }
  };
  const put = e => E.set(e.id, e);

  let _zoneDefs = [], _city = null, _ext = null;
  async function load() {
    const zj = await fetch(base + "/zones.json").then(r => r.json());
    _city = zj.city; _zoneDefs = zj.zones || [];
    try { const rj = await fetch(base + "/relationships.json").then(r => r.json()); (rj.relationships || []).forEach(r => REL.push(r)); } catch (e) {}
  }

  // scale the FRACTIONAL zone rects onto the real district bounding box, so zones/parcels sit exactly over the
  // buildings (not the whole AOI). Called by app.js with AICITY.districtBbox before registerBuildings().
  function setExtent(bbox) {
    if (!bbox || bbox.length !== 4) return;
    _ext = bbox.slice();
    const [W, S, E, N] = bbox, dx = E - W, dy = N - S;
    put(Object.assign({}, _city, { bbox: bbox.slice() }));
    for (const z of _zoneDefs) {
      const [fx0, fy0, fx1, fy1] = z.rect;
      const zb = [W + fx0 * dx, S + fy0 * dy, W + fx1 * dx, S + fy1 * dy];
      put(Object.assign({}, z, { bbox: zb }));
      // derive 2 parcels per zone by splitting the bbox on its longer axis (deterministic, no extra file)
      const [w, s, e, n] = zb, wide = (e - w) >= (n - s);
      const halves = wide ? [[w, s, (w + e) / 2, n], [(w + e) / 2, s, e, n]] : [[w, s, e, (s + n) / 2], [w, (s + n) / 2, e, n]];
      const zc = z.id.split("-")[1];
      halves.forEach((bb, i) => put({ id: `PARCEL-${zc}${i + 1}`, type: "parcel",
        name: `Parcel ${zc}${i + 1}`, parentId: z.id, function: z.function, bbox: bb, status: z.status }));
    }
    index();
  }

  // the renderer hands us its generated buildings (stable ids, centroids). We attach each to the parcel that
  // contains its centroid (fallback: the zone), so the City→Zone→Parcel→Building hierarchy is complete.
  function registerBuildings(list) {
    const parcels = [...E.values()].filter(e => e.type === "parcel");
    const zones = [...E.values()].filter(e => e.type === "zone");
    for (const b of list) {
      const host = parcels.find(p => inBbox(b.cx, b.cy, p.bbox))
        || zones.find(z => inBbox(b.cx, b.cy, z.bbox));
      put({
        id: b.id, type: "building", name: b.name || defaultName(b, host), parentId: host ? host.id : "AICITY",
        function: b.function || b.t, status: "planned",
        geometry: { center: [b.cx, b.cy], bbox: [b.cx - b.hw, b.cy - b.hh, b.cx + b.hw, b.cy + b.hh] },
        metadata: { heightM: Math.round(b.h), floors: Math.max(1, Math.round(b.h / 4)) },
        lifecycle: { status: "planned", plannedCompletion: "2028", percentComplete: 0 },
      });
    }
    index();
  }
  const TNAME = { tower: "AI / Office Tower", office: "Commercial Building", lab: "Innovation Lab", resi: "Residential Block", civic: "Civic Building", landmark: "AI Innovation Centre", utility: "Utility Structure" };
  const defaultName = (b, host) => (TNAME[b.t] || "Building") + (host ? " · " + host.name : "");

  // register infrastructure networks: each network is an entity, each node a `net-node` (parent = network),
  // each edge a "feeds" relationship. Node positions scale the fractional `f` onto the district extent.
  function registerNetworks(networks) {
    if (!_ext) return;
    const [W, S] = _ext, dx = _ext[2] - W, dy = _ext[3] - S;
    for (const net of networks || []) {
      put({ id: net.id, type: "network", name: net.name, parentId: null, function: net.kind, status: "planned", metadata: { color: net.color } });
      for (const n of net.nodes) {
        const c = [W + n.f[0] * dx, S + n.f[1] * dy];
        put({ id: n.id, type: "net-node", name: n.name, parentId: net.id, function: net.kind, status: "planned",
          geometry: { center: c }, metadata: { nodeType: n.type, network: net.kind } });
      }
      for (const [a, b] of net.edges) REL.push({ sourceId: a, relationshipType: "feeds", targetId: b });
    }
    index();
  }
  // auto-link every building to its NEAREST node in each network → a transformer knows the buildings it powers, etc.
  function linkBuildingsToNetworks(networks) {
    const RELNAME = { power: "poweredBy", water: "suppliedBy", fiber: "connectedTo", drainage: "drainsTo", mobility: "accessedBy" };
    const buildings = [...E.values()].filter(e => e.type === "building" && e.geometry && e.geometry.center);
    for (const net of networks || []) {
      const rel = RELNAME[net.kind] || "connectedTo";
      const nodes = net.nodes.map(n => get(n.id)).filter(n => n && n.geometry);
      if (!nodes.length) continue;
      for (const b of buildings) {
        const [bx, by] = b.geometry.center;
        let best = null, bd = Infinity;
        for (const n of nodes) { const [nx, ny] = n.geometry.center, d = (nx - bx) * (nx - bx) + (ny - by) * (ny - by); if (d < bd) { bd = d; best = n; } }
        if (best) REL.push({ sourceId: b.id, relationshipType: rel, targetId: best.id });
      }
    }
  }

  const get = id => E.get(id) || null;
  const getParent = id => { const e = get(id); return e && e.parentId ? get(e.parentId) : null; };
  const getChildren = id => (childrenIdx.get(id) || []).map(get).filter(Boolean);
  const getRelationships = id => REL.filter(r => r.sourceId === id || r.targetId === id);
  const getDependencies = id => REL.filter(r => r.sourceId === id).map(r => get(r.targetId)).filter(Boolean);
  const getDependents = id => REL.filter(r => r.targetId === id).map(r => get(r.sourceId)).filter(Boolean);
  const byLevel = type => [...E.values()].filter(e => e.type === type);
  const getRoot = () => get("AICITY");
  function ancestors(id) { const chain = []; let e = get(id); while (e) { chain.unshift(e.id); e = e.parentId ? get(e.parentId) : null; } return chain; }

  return { load, setExtent, registerBuildings, registerNetworks, linkBuildingsToNetworks, get, getRoot, getParent, getChildren, getRelationships, getDependencies, getDependents, byLevel, ancestors };
}
