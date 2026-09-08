// app.js — digital-twin entry (ES module). Waits for the render to publish window.AICITY, then builds the
// DigitalTwinState + Repository + shell UI and wires City→Zone→Parcel→Building drilldown THROUGH the AICITY
// bridge. The proven Cesium render is untouched; this layer owns the semantics (Rule #24).
import { createState } from "./twin/state.js";
import { createDemoRepository } from "./twin/repository.js";
import { mountShell } from "./twin/ui.js";

const whenReady = cb => { if (window.AICITY) cb(); else window.addEventListener("aicity-ready", cb, { once: true }); };

whenReady(async () => {
  const A = window.AICITY;
  const repo = createDemoRepository("./data");
  try { await repo.load(); } catch (e) { console.warn("[twin] demo data load failed", e); }
  // scale zones/parcels onto the ACTUAL district bounds (where the buildings are), not the whole AOI
  repo.setExtent(A.districtBbox || A.aoiBbox);
  repo.registerBuildings(A.buildings || []);           // attach the render's buildings to zones/parcels

  const state = createState({ simulationTime: performance.now() });

  // navigate to any entity: update state (selection + breadcrumb + level) and drive the scene via the bridge
  function navigate(id) {
    const e = repo.get(id); if (!e) return;
    if (A.interiorsActive && A.interiorsActive()) return;   // don't fight an active walk-through
    const chain = repo.ancestors(id);
    const level = ({ city: "city", zone: "zone", parcel: "parcel", building: "building" })[e.type] || "city";
    state.patch({ selectedEntityId: e.type === "city" ? null : id, breadcrumb: chain, spatialLevel: level });
    if (e.type === "building") { A.outline(null); A.highlight(id); A.flyToBuilding(id); }
    else if (e.type === "city") { A.frameCity(); }
    else if (e.bbox) { A.clearHighlight(); A.outline(e.bbox); A.flyToBbox(e.bbox); }
  }

  A.onPick = bid => navigate(bid);                     // single-click a building in the scene → select it

  mountShell({
    state, repo,
    onNavigate: navigate,
    onEnterBuilding: id => A.enterBuilding(id),         // inspector "Enter building →" (or double-click in scene)
  });

  console.log("[twin] AI City digital twin — Phase 1 shell ready:", repo.byLevel("building").length, "buildings,",
    repo.byLevel("zone").length, "zones,", repo.byLevel("parcel").length, "parcels");
});
