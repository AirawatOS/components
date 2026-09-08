// lenses/governance.js — governance rendered CONTEXTUALLY around the selected asset: who owns / operates /
// regulates it, the responsible department, data controller & access, approvals, and — for AI / compute assets —
// model governance (owner / version / purpose / risk category / evaluation / audit / approval authority).
// Deterministic synthetic data — illustrative.
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function seed(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967296; }
const row = (k, v) => `<div class="row"><span>${k}</span><b>${v}</b></div>`;

const OPERATOR = { datacentre: "AI City Compute Co (PPP)", utility: "City Utilities Dept", ai: "AI City Facilities", innovation: "Innovation Foundation", commercial: "AI City Facilities", residential: "Housing Board", government: "State IT Dept" };
const DEPT = { datacentre: "Digital Infrastructure", utility: "Public Works", ai: "Innovation & Tech", innovation: "Innovation & Tech", commercial: "Urban Development", residential: "Housing", government: "Administration" };

export const governance = {
  key: "governance",
  onActivate(ctx) { ctx.A.clearEmphasis && ctx.A.clearEmphasis(); },
  inspector(e, repo) {
    if (!e || e.type === "city" || !e.type) {
      return { title: "Governance — AI City",
        body: row("Operating authority", "Lucknow AI City Authority") + row("Land", "UPAVP") + row("Regulator", "UP Govt · MeitY (AI)")
          + row("Data controller", "AI City Authority") + `<div class="ill">Select an asset to see its ownership, operator, regulator, approvals and data governance.</div>` };
    }
    const fn = e.function || "commercial", r = seed(e.id);
    let body = row("Owner (land)", "UPAVP · plot 15/ITP")
      + row("Operator", OPERATOR[fn] || "AI City Facilities")
      + row("Responsible dept", DEPT[fn] || "Urban Development")
      + row("Regulator", fn === "datacentre" || fn === "ai" ? "MeitY · CEA (power)" : fn === "utility" ? "UPERC / Jal Nigam" : "UP RERA")
      + row("Can modify", "AI City Authority (with dept sign-off)")
      + row("Data controller", "AI City Authority")
      + row("Data access", r > 0.5 ? "Operator + Authority (role-based)" : "Authority only")
      + `<div class="sub">Approvals</div><div class="rels">`
      + ["Master plan ✓", "Building plan ✓", (r > 0.4 ? "Environmental clearance ✓" : "Environmental clearance ⏳"), "Fire NOC " + (r > 0.6 ? "✓" : "⏳")].map(a => `<div class="rel"><b>${esc(a)}</b></div>`).join("") + `</div>`;
    // AI-system governance for compute / AI assets
    if (fn === "datacentre" || fn === "ai") {
      const risk = r > 0.7 ? "High" : "Limited", ver = "v" + (1 + Math.floor(r * 3)) + "." + Math.floor(seed(e.id + "v") * 9);
      body += `<div class="sub">AI system governance</div>`
        + row("Model owner", "AI City Compute Co")
        + row("Version", ver) + row("Purpose", fn === "datacentre" ? "GPU orchestration / inference" : "Building intelligence")
        + row("Risk category", `<span class="pill ${risk === "High" ? "warning" : "normal"}">${risk}</span>`)
        + row("Evaluation", r > 0.5 ? "Passed (leave-station-out)" : "In review")
        + row("Last audit", "2031-0" + (1 + Math.floor(r * 6)))
        + row("Approval authority", "AI Oversight Board");
    }
    return { title: e.name || e.id, body: body + `<div class="ill">Illustrative governance — rendered around the selected asset</div>` };
  },
};
