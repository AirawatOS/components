// agents/agents.js — the Airawat intelligence layer over the twin. A roster of domain agents (+ an AI City
// Coordinator) and a PURE recommendations() that derives context-aware, deterministic recommendations from the
// current DigitalTwinState (transformer warning in OPERATE, delayed building in BUILD, active PLAN scenario, …).
// Mock advice for the demo; Phase-8 would bind these to the real Airawat decision spine + skills.
import { OP_BASE, WARN_NODE, reading } from "../sim/telemetry.js";
import { START, progress } from "../sim/build.js";

export const AGENTS = [
  { id: "coordinator",  name: "AI City Coordinator", icon: "◎", observes: ["all domains", "active recommendations"], reasons: ["cross-domain trade-offs", "priority"], recommend: ["what to act on first"], act: ["orchestrate the domain agents"] },
  { id: "planning",     name: "Planning Agent",      icon: "▤", observes: ["land use", "capacity", "scenarios"], reasons: ["demand vs capacity"], recommend: ["phasing", "site options"], act: ["draft plan revisions"] },
  { id: "construction", name: "Construction Agent",  icon: "⛭", observes: ["schedule", "progress"], reasons: ["variance", "critical path"], recommend: ["re-sequencing"], act: ["flag delays, raise RFIs"] },
  { id: "energy",       name: "Energy Agent",        icon: "⚡", observes: ["load", "transformer temp", "demand"], reasons: ["overload / thermal risk"], recommend: ["load shifting", "inspection"], act: ["issue a work order"] },
  { id: "water",        name: "Water Agent",         icon: "💧", observes: ["flow", "pressure"], reasons: ["leak risk"], recommend: ["valve operations"], act: ["dispatch a crew"] },
  { id: "compute",      name: "Compute Agent",       icon: "▦", observes: ["GPU utilisation", "PUE", "cooling"], reasons: ["thermal / utilisation limits"], recommend: ["workload rebalancing"], act: ["schedule inference jobs"] },
  { id: "mobility",     name: "Mobility Agent",      icon: "⛗", observes: ["traffic", "parking", "EV charging"], reasons: ["congestion"], recommend: ["signal timing"], act: ["adjust routing"] },
  { id: "building",     name: "Building Agent",      icon: "▣", observes: ["occupancy", "energy", "comfort"], reasons: ["efficiency"], recommend: ["set-points"], act: ["BMS control"] },
  { id: "environment",  name: "Environment Agent",   icon: "❋", observes: ["air quality", "heat", "green cover"], reasons: ["exposure"], recommend: ["mitigation"], act: ["raise alerts"] },
  { id: "maintenance",  name: "Maintenance Agent",   icon: "⚒", observes: ["asset health"], reasons: ["failure probability"], recommend: ["preventive maintenance"], act: ["schedule work"] },
];
export const AGENT = id => AGENTS.find(a => a.id === id);

export function recommendations(repo, state) {
  const recs = [], lc = state.lifecycleMode, t = state.simulationTime;
  if (lc === "operate") {
    const tx = repo.get(WARN_NODE);
    if (tx) { const r = reading(tx, t || OP_BASE);
      if (r.state !== "normal") recs.push({ agent: "energy", sev: r.state, title: tx.name + " approaching threshold",
        detail: `Load ${r.metrics[0].v}%, ${r.metrics[1].v} °C. Shift ≈12% load to TX-06 and schedule inspection within 48 h.`, focus: WARN_NODE }); }
    const hot = repo.byLevel("building").filter(b => b.function === "datacentre").find(b => reading(b, t || OP_BASE).state !== "normal");
    if (hot) recs.push({ agent: "compute", sev: "warning", title: hot.name + " GPU load high",
      detail: "Utilisation above 92%. Rebalance inference to another compute hall; verify cooling head-room.", focus: hot.id });
  }
  if (lc === "build") {
    const d = repo.byLevel("building").map(x => ({ x, p: progress(x, t || START, "actual") })).find(o => o.p.delayed && o.p.actualPct > 0.15 && o.p.actualPct < 1);
    if (d) recs.push({ agent: "construction", sev: "warning", title: d.x.name + " behind schedule",
      detail: `Variance ${d.p.variance}% vs plan. Expedite MEP and re-sequence fit-out to protect the completion date.`, focus: d.x.id });
  }
  if (lc === "plan" && state.activeScenario) recs.push({ agent: "planning", sev: "info", title: "Capacity scenario under review",
    detail: "Added GPU load raises grid-headroom use — verify the TX-06 feeder and cooling-water supply before committing.", focus: "ZONE-C" });
  recs.push({ agent: "water", sev: "info", title: "Leak-risk watch · Water Zone 03",
    detail: "Pressure variance within limits; continue nightly monitoring of WZ-03.", focus: "WATER-ZONE-03" });
  return recs;
}
