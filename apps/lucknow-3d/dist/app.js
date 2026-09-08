// app.js — digital-twin entry (ES module). Waits for the render to publish window.AICITY, then builds the
// DigitalTwinState + Repository + shell UI and wires City→Zone→Parcel→Building drilldown + infrastructure
// networks THROUGH the AICITY bridge. The proven Cesium render is untouched; this layer owns the semantics.
import { createState } from "./twin/state.js";
import { createDemoRepository } from "./twin/repository.js";
import { mountShell } from "./twin/ui.js";
import { createNetworkLayer } from "./layers/network.js";

const whenReady = cb => { if (window.AICITY) cb(); else window.addEventListener("aicity-ready", cb, { once: true }); };

whenReady(async () => {
  const A = window.AICITY;
  const repo = createDemoRepository("./data");
  try { await repo.load(); } catch (e) { console.warn("[twin] demo data load failed", e); }
  repo.setExtent(A.districtBbox || A.aoiBbox);          // scale zones/parcels onto the district
  repo.registerBuildings(A.buildings || []);

  // ---- infrastructure networks (Phase 2) ----
  let networks = [];
  try { networks = (await fetch("./data/networks.json").then(r => r.json())).networks || []; } catch (e) { console.warn("[twin] networks load failed", e); }
  repo.registerNetworks(networks);                      // nodes/edges become governed entities + "feeds" edges
  repo.linkBuildingsToNetworks(networks);               // every building → nearest node (poweredBy / suppliedBy / connectedTo)
  const netLayer = createNetworkLayer({ scene: A.scene, bbox: A.districtBbox || A.aoiBbox, networks, hideable: A.addHideable });

  const state = createState({ simulationTime: performance.now() });

  function navigate(id) {
    const e = repo.get(id); if (!e) return;
    if (A.interiorsActive && A.interiorsActive()) return;
    const chain = repo.ancestors(id);
    const level = ({ city: "city", zone: "zone", parcel: "parcel", building: "building" })[e.type] || "city";
    state.patch({ selectedEntityId: e.type === "city" ? null : id, breadcrumb: chain.length ? chain : ["AICITY"], spatialLevel: level });
    if (e.type === "building") { A.outline(null); A.highlight(id); A.flyToBuilding(id); }
    else if (e.type === "city") { A.frameCity(); }
    else if (e.type === "net-node" && e.geometry && e.geometry.center) { A.clearHighlight(); A.outline(null); A.flyToPoint(e.geometry.center[0], e.geometry.center[1]); }
    else if (e.type === "network") { A.frameCity(); }
    else if (e.bbox) { A.clearHighlight(); A.outline(e.bbox); A.flyToBbox(e.bbox); }
  }

  A.onPick = id => navigate(id);                        // clicking a building OR a network node selects it

  mountShell({ state, repo, networks, onNavigate: navigate, onEnterBuilding: id => A.enterBuilding(id) });

  // networks visibility is driven by state (toggle chips / presets)
  let lastNets = "";
  state.subscribe(s => { const key = s.activeNetworks.slice().sort().join(","); if (key !== lastNets) { lastNets = key; netLayer.setActive(s.activeNetworks); } });

  console.log("[twin] Phase 2 ready —", repo.byLevel("building").length, "buildings,",
    repo.byLevel("net-node").length, "network nodes across", networks.length, "networks");
});
