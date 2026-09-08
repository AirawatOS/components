// lenses/digital.js — the INVISIBLE digital fabric made visible. On activate it lights up the fibre network; the
// inspector becomes an AIRAWAT overlay for the selected asset: connected DATA sources, MODELS, and AGENTS.
// Synthetic + illustrative (Phase 7 binds agents to the real decision spine).
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// Airawat connectivity by building function (deterministic)
function airawat(fn) {
  const base = { data: ["BMS", "Power meter", "Water meter", "Occupancy"], models: ["Energy forecast"], agents: ["Building Agent", "Energy Agent"] };
  const by = {
    datacentre: { data: ["GPU telemetry", "PDU / power", "Cooling sensors", "Network flow"], models: ["Load forecast", "Cooling optimisation", "Failure prediction"], agents: ["Compute Agent", "Energy Agent", "Maintenance Agent"] },
    ai:         { data: ["BMS", "Power meter", "Occupancy", "Air quality"], models: ["Energy forecast", "Occupancy prediction"], agents: ["Building Agent", "Energy Agent"] },
    innovation: { data: ["BMS", "Lab sensors", "Power meter"], models: ["Energy forecast", "Utilisation"], agents: ["Building Agent"] },
    residential:{ data: ["Smart meter", "Water meter", "Occupancy"], models: ["Demand forecast"], agents: ["Building Agent"] },
    utility:    { data: ["SCADA", "Flow / pressure", "Power meter"], models: ["Load forecast", "Leak / fault detection"], agents: ["Energy Agent", "Water Agent", "Maintenance Agent"] },
  };
  return by[fn] || base;
}
const chips = (label, arr) => `<div class="sub">${label}</div><div class="rels">` + arr.map(x => `<div class="rel"><b>${esc(x)}</b></div>`).join("") + `</div>`;

export const digital = {
  key: "digital",
  onActivate(ctx) {
    const cur = ctx.state.get().activeNetworks;
    if (cur.indexOf("fiber") < 0) { this._added = true; ctx.state.patch({ activeNetworks: cur.concat("fiber") }); }
  },
  onDeactivate(ctx) {
    if (this._added) { this._added = false; ctx.state.patch({ activeNetworks: ctx.state.get().activeNetworks.filter(k => k !== "fiber") }); }
  },
  inspector(e, repo) {
    if (e && e.type === "building") {
      const a = airawat(e.function);
      return { title: e.name || "Building",
        body: `<div class="dim" style="margin-bottom:6px">Airawat connectivity for this asset:</div>`
          + chips("Data sources", a.data) + chips("Models", a.models) + chips("Agents", a.agents)
          + `<div class="ill">Illustrative — the digital twin's data / model / agent graph</div>` };
    }
    if (e && e.type === "net-node") return { title: e.name || "Node",
      body: `<div class="dim">Digital-fabric node — ${esc(e.metadata && e.metadata.network || "network")}. Its edges are shown on the map; select a building to see its Airawat data, models and agents.</div>` };
    // overview
    const edges = repo.byLevel("net-node").filter(n => n.function === "fiber").length;
    return { title: "Digital fabric — AI City",
      body: `<div class="dim">The fibre backbone is shown on the map. Every asset streams to Airawat OS: sensors → IoT gateways → event platform → models → agents → city systems.</div>`
        + `<div class="sub">Digital backbone</div>` + `<div class="row"><span>Fibre nodes</span><b>${edges}</b></div>`
        + `<div class="ill">Select a building to see its data / models / agents</div>` };
  },
};
