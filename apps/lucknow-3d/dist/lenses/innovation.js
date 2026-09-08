// lenses/innovation.js — the innovation ECOSYSTEM (not infrastructure). On activate it emphasises the AI /
// innovation buildings and frames the Innovation District; the inspector shows organizations, labs, active pilots,
// research domains and AI models per building. Deterministic synthetic data — illustrative.
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function seed(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967296; }
const DOMAINS = ["Urban Mobility", "Climate", "Health", "Agriculture", "Language AI", "Energy", "Water", "Fintech", "Robotics"];
const isInno = b => b.function === "ai" || b.function === "innovation";
const row = (k, v) => `<div class="row"><span>${k}</span><b>${v}</b></div>`;

export function profile(e) {
  const r = seed(e.id), r2 = seed(e.id + "b"), r3 = seed(e.id + "c");
  const startups = 6 + Math.floor(r * 42), labs = 2 + Math.floor(r2 * 9), pilots = 3 + Math.floor(r3 * 13);
  const n = 3 + Math.floor(r * 3), start = Math.floor(r2 * DOMAINS.length);
  const domains = Array.from({ length: n }, (_, i) => DOMAINS[(start + i) % DOMAINS.length]);
  return { startups, labs, pilots, domains, models: 2 + Math.floor(r3 * 8), sandboxes: 1 + Math.floor(r * 6) };
}

export const innovation = {
  key: "innovation",
  onActivate(ctx) {
    ctx.A.emphasize(ctx.repo.byLevel("building").filter(isInno).map(b => b.id));
    const z = ctx.repo.get("ZONE-B"); if (z && z.bbox) ctx.A.flyToBbox(z.bbox);
  },
  onDeactivate(ctx) { ctx.A.clearEmphasis(); },
  inspector(e, repo) {
    if (e && e.type === "building" && isInno(e)) {
      const p = profile(e);
      return { title: e.name || "Innovation building",
        body: row("Startups", p.startups) + row("Research labs", p.labs) + row("Active pilots", p.pilots)
          + row("AI models in dev", p.models) + row("Sandbox deployments", p.sandboxes)
          + `<div class="sub">Domains</div><div class="rels">` + p.domains.map(d => `<div class="rel"><b>${esc(d)}</b></div>`).join("") + `</div>`
          + `<div class="ill">Illustrative innovation ecosystem</div>` };
    }
    if (e && e.type === "building") return { title: e.name || "Building",
      body: `<div class="dim">Not an innovation node. The Innovation lens maps the ecosystem — startups, labs, pilots and AI models — hosted in the AI &amp; Innovation districts. Select one of those buildings.</div>` };
    const list = repo.byLevel("building").filter(isInno);
    const agg = list.reduce((a, b) => { const p = profile(b); a.s += p.startups; a.l += p.labs; a.pi += p.pilots; a.m += p.models; return a; }, { s: 0, l: 0, pi: 0, m: 0 });
    return { title: "Innovation — AI City",
      body: row("Innovation buildings", list.length) + row("Startups", agg.s) + row("Research labs", agg.l)
        + row("Active pilots", agg.pi) + row("AI models in dev", agg.m)
        + `<div class="sub">Innovation buildings</div><div class="list">`
        + list.slice(0, 20).map(b => `<button class="li" data-id="${esc(b.id)}">${esc(b.name)}</button>`).join("") + `</div>`
        + `<div class="ill">Illustrative synthetic ecosystem</div>` };
  },
};
