// sim/scenario.js — PLAN scenarios + a deterministic impact model. PURE logic (no Cesium): given the baseline
// compute aggregate and a scenario delta, it returns illustrative impacts (power / cooling / water / grid / carbon
// / jobs / land). Config-driven coefficients — all clearly ILLUSTRATIVE for the demo.
export const SCENARIOS = [
  { id: "baseline", name: "Baseline Masterplan" },
  { id: "gpu20", name: "+20 MW GPU", add: { itMW: 20 } },
  { id: "gpu50", name: "+50 MW GPU", add: { itMW: 50 } },
];

const C = {
  pue: 1.25,                 // facility power = IT × PUE
  coolPerIT: 0.35,           // cooling load as a fraction of IT load (MW)
  waterLperKWh: 1.8,         // cooling water (litres per kWh of IT)
  gridSupplyMW: 220,         // assumed city grid supply (for % headroom)
  carbonTperMWh: 0.71,       // India grid emission factor (tCO2 / MWh)
  jobsPerMW: 9,              // direct + ecosystem jobs per MW IT
  haPerMW: 0.12,             // land per MW IT (hectares)
};

/** @param {{powerMW:number}} base  @param {object} sc  → impacts object */
export function impacts(base, sc) {
  const addIT = (sc && sc.add && sc.add.itMW) || 0;
  const facility = addIT * C.pue;
  const r1 = x => Math.round(x * 10) / 10;
  return {
    addIT,
    facilityMW: r1(facility),
    coolingMW: r1(addIT * C.coolPerIT),
    waterM3d: Math.round(addIT * 24 * C.waterLperKWh),          // MW×24h×L/kWh → m³/day
    gridPct: r1(facility / C.gridSupplyMW * 100),
    carbonKtY: Math.round(facility * 8760 * C.carbonTperMWh / 1000),
    jobs: Math.round(addIT * C.jobsPerMW),
    landHa: r1(addIT * C.haPerMW),
    basePowerMW: base ? r1(base.powerMW) : null,
    projPowerMW: base ? r1(base.powerMW + facility) : null,
  };
}
