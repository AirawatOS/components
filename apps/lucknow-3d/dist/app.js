// app.js — digital-twin entry (ES module). Waits for the render to publish window.AICITY, then builds the
// DigitalTwinState + Repository + shell UI and wires City→Zone→Parcel→Building drilldown + infrastructure
// networks THROUGH the AICITY bridge. The proven Cesium render is untouched; this layer owns the semantics.
import { createState } from "./twin/state.js";
import { createDemoRepository } from "./twin/repository.js";
import { mountShell } from "./twin/ui.js";
import { createNetworkLayer } from "./layers/network.js";
import { compute, specs as computeSpecs } from "./lenses/compute.js";
import { digital } from "./lenses/digital.js";
import { SCENARIOS, impacts } from "./sim/scenario.js";
import { START, END, progress, colorFor } from "./sim/build.js";
import { OP_BASE, WARN_NODE, reading, affected, agentAdvice } from "./sim/telemetry.js";
import { AGENTS, recommendations } from "./agents/agents.js";
import { mountAirawat } from "./twin/airawat.js";

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

  // BUILD inspector (construction status of a selected building at the timeline date)
  const monthLabel = ms => { const d = new Date(ms); return d.getUTCFullYear() + "-" + String(d.getUTCMonth() + 1).padStart(2, "0"); };
  const buildInspector = (e, s) => {
    const p = progress(e, s.simulationTime || START, s.buildMode), pct = m => Math.round(m * 100) + "%";
    const row = (k, v) => `<div class="row"><span>${k}</span><b>${v}</b></div>`;
    const rc = p.risk === "HIGH" ? "#e8896b" : p.risk === "MEDIUM" ? "#e8b84b" : "#39d3e0";
    return { title: e.name || "Building",
      body: row("Planned", monthLabel(p.plannedStart) + " → " + monthLabel(p.plannedEnd)) + row("Stage", p.stage)
        + row("Planned progress", pct(p.plannedPct)) + row("Actual progress", pct(p.actualPct))
        + `<div class="row"><span>Variance</span><b style="color:${p.variance < 0 ? "#e8896b" : "#96d886"}">${p.variance > 0 ? "+" : ""}${p.variance}%</b></div>`
        + `<div class="row"><span>Risk</span><b style="color:${rc}">${p.risk}</b></div>`
        + (p.delayed ? `<div class="ill" style="color:#e8b84b">⚠ Behind schedule — flagged by the Construction Agent.</div>` : `<div class="ill">Illustrative construction model</div>`) };
  };

  // OPERATE inspector (live telemetry for a selected asset; Energy-Agent card on the warning transformer)
  const operateInspector = (e, s) => {
    const t = s.simulationTime || OP_BASE, r = reading(e, t);
    const row = m => `<div class="row"><span>${m.k}</span><b>${m.v}${m.unit ? " " + m.unit : ""}</b></div>`;
    let body = `<div class="row"><span>State</span><b><span class="pill ${r.state}">${r.state.toUpperCase()}</span></b></div>` + r.metrics.map(row).join("");
    if (e.id === WARN_NODE) { const a = agentAdvice(e, t);
      body += `<div class="agent"><div class="an">✦ ${a.agent}</div><div class="as">${a.summary}</div><ul>${a.recommend.map(x => `<li>${x}</li>`).join("")}</ul></div>`;
    }
    return { title: e.name || e.id, body: body + `<div class="ill">Illustrative live telemetry</div>` };
  };

  // ---- semantic lenses (Phase 3): Compute + Digital wired; Innovation/Governance still stubs ----
  const LENSES = { compute, digital };
  const lensCtx = { A, repo, state };
  mountShell({ state, repo, networks, lenses: LENSES, buildInspector, operateInspector, onNavigate: navigate, onEnterBuilding: id => A.enterBuilding(id) });

  // ---- Airawat agents overlay (Phase 7): coordinator + domain agents + live recommendations ----
  mountAirawat({ state, repo, agents: AGENTS, recommendations, onFocus: id => id && navigate(id) });

  // networks visibility is driven by state (toggle chips / presets)
  let lastNets = "";
  state.subscribe(s => { const key = s.activeNetworks.slice().sort().join(","); if (key !== lastNets) { lastNets = key; netLayer.setActive(s.activeNetworks); } });

  // lens on-activate / on-deactivate hooks (emphasis, fibre, camera)
  let curLens = "physical";
  state.subscribe(s => {
    if (s.lens === curLens) return;
    const prev = LENSES[curLens]; if (prev && prev.onDeactivate) prev.onDeactivate(lensCtx);
    curLens = s.lens;
    const nxt = LENSES[curLens]; if (nxt && nxt.onActivate) nxt.onActivate(lensCtx);
  });

  // ---- PLAN scenarios (Phase 4) ----
  const $ = id => document.getElementById(id);
  const bar = $("ov-scenario");
  bar.innerHTML = SCENARIOS.map(s => `<button data-sc="${s.id}" class="${s.id === "baseline" ? "clr" : ""}">${s.name}</button>`).join("");
  bar.querySelectorAll("button").forEach(b => b.onclick = () => state.patch({ activeScenario: b.dataset.sc === "baseline" ? null : b.dataset.sc }));

  let ghost = null;
  const Cz = () => window.Cesium;
  function makeGhost(bbox, itMW) {
    const C = Cz(), cx = (bbox[0] + bbox[2]) / 2, cy = (bbox[1] + bbox[3]) / 2, s = 0.00016 * (1 + itMW / 45);
    const ll = [cx - s, cy - s, cx + s, cy - s, cx + s, cy + s, cx - s, cy + s, cx - s, cy - s];
    const inst = new C.GeometryInstance({ geometry: new C.PolygonGeometry({ polygonHierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(ll)), extrudedHeight: 22 + itMW * 0.25, height: 0, vertexFormat: C.PerInstanceColorAppearance.VERTEX_FORMAT }),
      attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(C.Color.fromBytes(120, 220, 244, 140)) } });
    const prim = new C.Primitive({ asynchronous: false, geometryInstances: inst, appearance: new C.PerInstanceColorAppearance({ flat: false, closed: false, translucent: true }) });
    A.scene.primitives.add(prim); A.addHideable(prim); return prim;
  }
  function applyScenario(id) {
    if (ghost) { try { A.scene.primitives.remove(ghost); } catch (e) {} ghost = null; }
    const panel = $("dt-scenario-panel");
    const sc = SCENARIOS.find(s => s.id === id);
    if (!sc || !sc.add) { panel.hidden = true; A.scene.requestRender(); return; }
    const dcs = repo.byLevel("building").filter(b => b.function === "datacentre");
    const base = dcs.reduce((a, b) => { a.powerMW += computeSpecs(b).powerMW; return a; }, { powerMW: 0 });
    const m = impacts(base, sc);
    const z = repo.get("ZONE-C"); if (z && z.bbox) { ghost = makeGhost(z.bbox, m.addIT); A.flyToBbox(z.bbox); }
    const row = (k, v, hi) => `<div class="row${hi ? " hi" : ""}"><span>${k}</span><b>${v}</b></div>`;
    panel.innerHTML = `<div class="t">${sc.name}</div><div class="st">Illustrative impact vs baseline</div>`
      + row("Added IT load", m.addIT + " MW")
      + row("Facility power", "+" + m.facilityMW + " MW", true)
      + row("City power", (m.basePowerMW != null ? m.basePowerMW + " → " + m.projPowerMW + " MW" : "+" + m.facilityMW + " MW"))
      + row("Grid headroom used", "+" + m.gridPct + "%")
      + row("Cooling load", "+" + m.coolingMW + " MW")
      + row("Cooling water", "+" + m.waterM3d.toLocaleString("en-IN") + " m³/day")
      + row("Carbon", "+" + m.carbonKtY + " ktCO₂/y")
      + row("Jobs", "+" + m.jobs.toLocaleString("en-IN"))
      + row("Land", "+" + m.landHa + " ha")
      + `<div class="ill">Deterministic synthetic model (config coefficients) — for illustration.</div>`;
    panel.hidden = false; A.scene.requestRender();
  }

  // show the scenarios bar only in PLAN; reflect active chip; recompute on scenario change
  let lastSc = " ", lastLc = "";
  state.subscribe(s => {
    if (s.lifecycleMode !== lastLc) { lastLc = s.lifecycleMode;
      const plan = s.lifecycleMode === "plan"; $("ov-scenario-h").hidden = !plan; $("ov-scenario").hidden = !plan;
      if (!plan && s.activeScenario) state.patch({ activeScenario: null });
    }
    const active = s.activeScenario || "baseline";
    bar.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.sc === active));
    if (s.activeScenario !== lastSc) { lastSc = s.activeScenario; applyScenario(active); }
  });
  { const plan = state.get().lifecycleMode === "plan"; $("ov-scenario-h").hidden = !plan; $("ov-scenario").hidden = !plan; }

  // ---- BUILD timeline (Phase 5): slider + Planned/Actual/Compare; colours the massing by construction stage ----
  const MSMO = 2629800000, months = Math.round((END - START) / MSMO);
  const bmode = document.querySelector("#dt-build .bmode");
  bmode.innerHTML = [["planned", "Planned"], ["actual", "Actual"], ["compare", "Compare"]].map(([k, l]) => `<button data-bm="${k}">${l}</button>`).join("");
  bmode.querySelectorAll("button").forEach(b => b.onclick = () => state.patch({ buildMode: b.dataset.bm }));
  const slider = $("dt-buildslider"); slider.max = String(months);
  const dateOf = m => START + m * MSMO;
  slider.oninput = () => state.patch({ simulationTime: dateOf(+slider.value) });

  let lastBuildKey = "";
  state.subscribe(s => {
    const inBuild = s.lifecycleMode === "build";
    $("dt-build").hidden = !inBuild;
    if (!inBuild) { if (lastBuildKey !== "") { lastBuildKey = ""; A.clearPaint(); } return; }
    if (s.simulationTime < START || s.simulationTime > END) {        // seed a sensible date on first entry
      const m = Math.round(months * 0.45); slider.value = String(m); state.patch({ simulationTime: dateOf(m) }); return;
    }
    slider.value = String(Math.round((s.simulationTime - START) / MSMO));
    bmode.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.bm === s.buildMode));
    $("dt-builddate").textContent = monthLabel(s.simulationTime);
    const key = s.simulationTime + "|" + s.buildMode;
    if (key !== lastBuildKey) { lastBuildKey = key; A.paintBuildings(id => colorFor(repo.get(id), s.simulationTime, s.buildMode)); }
  });

  console.log("[twin] Phase 5 ready — BUILD timeline over", months, "months");

  // ---- OPERATE live telemetry (Phase 6) ----
  const opFmt = ms => { const d = new Date(ms); const p = n => String(n).padStart(2, "0");
    return d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate()) + " " + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()); };
  const pauseBtn = $("dt-oppause"), alertBtn = $("dt-opalert");
  let opTimer = null, opPaused = false, opAffected = null, opNetAdded = false, opOn = false;
  pauseBtn.onclick = () => { opPaused = !opPaused; pauseBtn.textContent = opPaused ? "▶" : "⏸"; };
  alertBtn.onclick = () => navigate(WARN_NODE);
  const opPaint = () => { if (!opAffected) opAffected = new Set(affected(repo)); A.paintBuildings(id => opAffected.has(id) ? [232, 160, 70, 255] : null); };
  function startOperate() {
    opAffected = new Set(affected(repo));
    if (!opNetAdded && state.get().activeNetworks.indexOf("power") < 0) { opNetAdded = true; state.patch({ activeNetworks: state.get().activeNetworks.concat("power") }); }
    alertBtn.hidden = false; alertBtn.textContent = "⚠ " + WARN_NODE + " · Warning";
    if (!opTimer) opTimer = setInterval(() => { if (!opPaused) state.patch({ simulationTime: (state.get().simulationTime || OP_BASE) + 1800000 }); }, 1500);
    opPaint();
  }
  function stopOperate() {
    if (opTimer) { clearInterval(opTimer); opTimer = null; }
    alertBtn.hidden = true; A.clearPaint(); opAffected = null;
    if (opNetAdded) { opNetAdded = false; state.patch({ activeNetworks: state.get().activeNetworks.filter(k => k !== "power") }); }
  }
  state.subscribe(s => {
    const inOp = s.lifecycleMode === "operate";
    $("dt-operate").hidden = !inOp;
    if (inOp) {
      if (s.simulationTime < OP_BASE) { state.patch({ simulationTime: OP_BASE }); return; }   // seed the operational clock
      if (!opOn) { opOn = true; startOperate(); }
      $("dt-opclock").textContent = opFmt(s.simulationTime); opPaint();
    } else if (opOn) { opOn = false; stopOperate(); }
  });

  console.log("[twin] Phase 6 ready — OPERATE telemetry; warning on", WARN_NODE);
});
