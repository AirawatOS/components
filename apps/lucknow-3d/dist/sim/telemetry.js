// sim/telemetry.js — OPERATE: a synthetic, slowly-varying telemetry model. PURE: reading(entity, tMs) → metrics +
// operational state (normal/warning/critical). One transformer is deliberately in WARNING so the Energy Agent has
// something to explain. Values drift smoothly (sin over a ~30-min sim period). Illustrative — no real feed.
export const OP_BASE = Date.UTC(2031, 2, 21, 11, 0, 0);      // "operational city" clock start
export const WARN_NODE = "TX-04";                             // the transformer running hot (Compute District feeder)

function seed(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967296; }
const osc = (t, id, period) => Math.sin(t / (period || 1.8e6) + seed(id) * 6.283);   // −1..1, smooth
const r1 = x => Math.round(x * 10) / 10;

export function reading(e, t) {
  const M = (k, v, unit) => ({ k, v, unit });
  if (e.type === "net-node") {
    if (e.function === "power") {
      const warn = e.id === WARN_NODE;
      const load = warn ? 90 + 2.5 * osc(t, e.id, 9e5) : Math.round(45 + seed(e.id) * 28 + 5 * osc(t, e.id));
      const temp = r1(38 + load * 0.5 + 2 * osc(t, e.id + "T"));
      const state = load >= 95 ? "critical" : load >= 85 ? "warning" : "normal";
      return { state, metrics: [M("Load", Math.round(load), "%"), M("Oil temp", temp, "°C"), M("Health", warn ? 82 : 97, "%")] };
    }
    if (e.function === "water") return { state: "normal", metrics: [M("Flow", Math.round(55 + 18 * osc(t, e.id)), "L/s"), M("Pressure", r1(3.2 + 0.3 * osc(t, e.id + "p")), "bar")] };
    return { state: "normal", metrics: [M("Throughput", Math.round(40 + 30 * (0.5 + 0.5 * osc(t, e.id))), "Gbps")] };
  }
  if (e.type === "building") {
    if (e.function === "datacentre") {
      const util = Math.round(55 + 28 * (0.5 + 0.5 * osc(t, e.id, 1.2e6)));
      return { state: util > 92 ? "warning" : "normal", metrics: [M("GPU utilisation", util, "%"), M("IT power", r1(0.6 + util / 100 * 2.4), "MW"), M("PUE", r1(1.22 + 0.06 * osc(t, e.id + "e")), ""), M("Cold-aisle", r1(22 + 1.5 * osc(t, e.id + "c")), "°C")] };
    }
    const occ = Math.round(30 + 45 * (0.5 + 0.5 * osc(t, e.id, 2.4e6)));
    return { state: "normal", metrics: [M("Occupancy", occ, "%"), M("Energy", Math.round(40 + occ * 3 + 20 * osc(t, e.id + "e")), "kW"), M("Water", r1(2 + occ / 30), "m³/d"), M("Indoor", r1(23 + 1.2 * osc(t, e.id + "i")), "°C")] };
  }
  return { state: "normal", metrics: [] };
}

// buildings served by the warning transformer (poweredBy edge) — the "affected" set
export function affected(repo) { return repo.getDependents(WARN_NODE).filter(x => x && x.type === "building").map(x => x.id); }

// the Energy Agent's read on the warning transformer (deterministic; mock recommendation)
export function agentAdvice(node, t) {
  const r = reading(node, t), load = r.metrics[0].v, temp = r.metrics[1].v;
  return {
    agent: "Energy Agent",
    summary: `${node.name} is approaching its safe operating threshold (load ${load}%, ${temp} °C).`,
    recommend: ["Shift ≈12% load to TX-06.", "Schedule inspection within 48 hours."],
    impact: [WARN_NODE, "TX-06"],
  };
}
