// sim/build.js — the BUILD (construction) model. PURE logic: every building gets a deterministic schedule
// (planned start/finish, duration) + an "actual" progress that lags for a subset (one runs clearly late). Given a
// date it yields planned%/actual%/status/variance/risk, and a colour for the massing. Illustrative, config-driven.
export const START = Date.UTC(2026, 0, 1);
export const END = Date.UTC(2031, 0, 1);
const MS_MONTH = 2629800000;
const clamp01 = v => Math.max(0, Math.min(1, v));
function seed(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967296; }

// zones phase in over time: utilities + compute first, AI core next, the rest later
const PHASE = { utility: 0, datacentre: 2, ai: 5, government: 7, innovation: 9, commercial: 12, residential: 14 };

export function schedule(e) {
  const fn = e.function || "", r = seed(e.id);
  const startM = (PHASE[fn] != null ? PHASE[fn] : 10) + Math.floor(r * 8);
  const durM = 12 + Math.floor(seed(e.id + "d") * 20);            // 12–31 months
  const plannedStart = START + startM * MS_MONTH;
  return { plannedStart, plannedEnd: plannedStart + durM * MS_MONTH, durM, delayed: seed(e.id + "x") > 0.82 };
}

export const STAGES = [[0, "Not started", [70, 78, 100]], [0.08, "Foundation", [232, 150, 74]], [0.30, "Structure", [236, 184, 90]],
  [0.55, "Envelope", [90, 150, 200]], [0.75, "MEP", [70, 120, 170]], [0.90, "Fit-out", [150, 200, 220]]];
function stage(pct) { let s = STAGES[0]; for (const st of STAGES) if (pct >= st[0]) s = st; return pct >= 1 ? [1, "Complete", null] : s; }

export function progress(e, date, mode) {
  const s = schedule(e);
  const plannedPct = clamp01((date - s.plannedStart) / (s.plannedEnd - s.plannedStart));
  const actualPct = clamp01(plannedPct * (s.delayed ? 0.78 : 0.96));
  const shown = mode === "actual" || mode === "compare" ? actualPct : plannedPct;
  const st = stage(shown);
  const variance = Math.round((actualPct - plannedPct) * 100);
  const risk = (s.delayed && plannedPct > 0.15 && actualPct < 1) ? "HIGH" : (variance <= -8 ? "MEDIUM" : "LOW");
  return { plannedPct, actualPct, stage: st[1], stageColor: st[2], variance, risk, delayed: s.delayed,
    plannedStart: s.plannedStart, plannedEnd: s.plannedEnd, complete: shown >= 1 };
}

// massing colour at a date: null = restore the building's own (land-use) colour when complete.
export function colorFor(e, date, mode) {
  const p = progress(e, date, mode);
  if (mode === "compare") {
    if (p.actualPct >= 1) return null;
    if (p.plannedPct <= 0) return [60, 66, 86, 120];               // not started yet
    if (p.variance <= -8) return [220, 96, 96];                    // behind
    if (p.variance >= 5) return [96, 210, 130];                    // ahead
    return [150, 170, 120];                                        // on track
  }
  return p.complete ? null : p.stageColor.concat(p.plannedPct <= 0 ? 120 : 255);
}
