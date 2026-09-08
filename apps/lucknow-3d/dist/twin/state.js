// twin/state.js — the single DigitalTwinState. All visualization + UI is driven from here (no boolean soup).
// Tiny observable: get() / patch(partial) / subscribe(fn)->unsub. Cesium clock stays separate from business state.

/** @typedef {Object} DigitalTwinState
 * @property {'plan'|'build'|'operate'} lifecycleMode
 * @property {'physical'|'compute'|'digital'|'innovation'|'governance'} lens
 * @property {'city'|'zone'|'parcel'|'building'|'floor'|'room'|'asset'} spatialLevel
 * @property {string|null} selectedEntityId
 * @property {string[]} breadcrumb          // ancestor id chain, root→…→selected
 * @property {string[]} activeNetworks
 * @property {string|null} activeScenario
 * @property {number} simulationTime        // epoch ms
 * @property {null|'planned-vs-actual'} compareMode */

const initial = {
  lifecycleMode: "plan",
  lens: "physical",
  spatialLevel: "city",
  selectedEntityId: null,
  breadcrumb: ["AICITY"],
  activeNetworks: [],
  activeScenario: null,
  simulationTime: 0,          // set at boot (Date.now unavailable in some sandboxes at import time)
  compareMode: null,
  buildMode: "planned",       // BUILD lifecycle view: 'planned' | 'actual' | 'compare'
};

export function createState(seed) {
  let s = Object.assign({}, initial, seed || {});
  const subs = new Set();
  const emit = () => subs.forEach(fn => { try { fn(s); } catch (e) { console.warn(e); } });
  return {
    get: () => s,
    patch(partial) {
      const next = Object.assign({}, s, partial);
      // shallow no-op guard: skip emit if nothing actually changed
      let changed = false;
      for (const k in partial) if (s[k] !== partial[k]) { changed = true; break; }
      s = next;
      if (changed) emit();
      return s;
    },
    subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
  };
}
