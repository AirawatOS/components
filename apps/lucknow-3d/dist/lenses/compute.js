// lenses/compute.js — AI City AS A COMPUTE SYSTEM. On activate it emphasises the data-centre buildings and frames
// the Compute District; the inspector shows designed GPU capacity / power / cooling (PLAN). Synthetic + illustrative.
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// deterministic per-asset spec from its stable id (FNV-1a → [0,1))
function seed(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967296; }
export function specs(e) {
  const r = seed(e.id), gpus = 512 * (1 + Math.floor(r * 7));           // 512 … 4096 GPUs
  const powerMW = Math.round(gpus * 0.0009 * 10) / 10;                  // ~0.9 kW/GPU incl. overhead
  const coolingMW = Math.round(powerMW * 0.38 * 10) / 10;
  const pue = Math.round((1.18 + r * 0.12) * 100) / 100;
  return { gpus, powerMW, coolingMW, pue, racks: Math.round(gpus / 32) };
}
const dcs = repo => repo.byLevel("building").filter(b => b.function === "datacentre");
const row = (k, v) => `<div class="row"><span>${k}</span><b>${v}</b></div>`;

export const compute = {
  key: "compute",
  onActivate(ctx) {
    const ids = dcs(ctx.repo).map(b => b.id);
    ctx.A.emphasize(ids);                                              // brighten data centres, dim the rest
    const z = ctx.repo.get("ZONE-C"); if (z && z.bbox) ctx.A.flyToBbox(z.bbox);
  },
  onDeactivate(ctx) { ctx.A.clearEmphasis(); },
  inspector(e, repo) {
    if (e && e.function === "datacentre") {
      const s = specs(e);
      return { title: e.name || "Data centre",
        body: row("Designed GPUs", s.gpus.toLocaleString("en-IN")) + row("Racks", s.racks)
          + row("Power demand", s.powerMW + " MW") + row("Cooling load", s.coolingMW + " MW") + row("Design PUE", s.pue)
          + `<div class="ill">Illustrative — designed capacity (PLAN). Live utilisation arrives in OPERATE.</div>` };
    }
    if (e && e.type === "building") return { title: e.name || "Building",
      body: `<div class="dim">Not a compute asset. The Compute lens designs the city's AI compute — data centres, GPU clusters, and their power &amp; cooling. Select a data centre (Compute District) to see its capacity.</div>` };
    // aggregate (no selection)
    const list = dcs(repo), agg = list.reduce((a, b) => { const s = specs(b); a.gpus += s.gpus; a.p += s.powerMW; a.c += s.coolingMW; return a; }, { gpus: 0, p: 0, c: 0 });
    return { title: "Compute — AI City",
      body: row("Data centres", list.length) + row("Designed GPUs", agg.gpus.toLocaleString("en-IN"))
        + row("Total power", Math.round(agg.p * 10) / 10 + " MW") + row("Total cooling", Math.round(agg.c * 10) / 10 + " MW")
        + `<div class="sub">Data centres</div><div class="list">`
        + list.slice(0, 20).map(b => `<button class="li" data-id="${esc(b.id)}">${esc(b.name)}</button>`).join("") + `</div>`
        + `<div class="ill">Illustrative synthetic capacity</div>` };
  },
};
