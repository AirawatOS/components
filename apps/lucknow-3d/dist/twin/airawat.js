// twin/airawat.js — the "Airawat OS" overlay: a launcher + panel showing the AI City Coordinator, the live
// context-aware recommendations (click → focus the affected asset), and the domain-agent roster (click → what each
// agent observes / reasons about / can recommend / can act on). Pure function of state; re-renders on change.
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const $ = id => document.getElementById(id);

export function mountAirawat({ state, repo, agents, recommendations, onFocus }) {
  const btn = $("dt-aibtn"), menu = $("dt-aimenu");
  const AGENT = id => agents.find(a => a.id === id);
  let openAgent = null;
  if (!btn || !menu) return;

  btn.onclick = e => { e.stopPropagation(); const open = menu.hidden; menu.hidden = !open; btn.classList.toggle("on", open); if (open) render(state.get()); };
  document.addEventListener("click", e => { if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) { menu.hidden = true; btn.classList.remove("on"); } });

  function render(s) {
    const recs = recommendations(repo, s);
    const co = AGENT("coordinator");
    let html = `<div class="aihd"><span class="ico">✦</span> Airawat OS <span class="sub">intelligence layer</span></div>`;
    html += `<div class="coord"><span class="ico">${co.icon}</span> ${esc(co.name)} · <b>${recs.length}</b> active recommendation${recs.length === 1 ? "" : "s"}</div>`;
    html += `<div class="sec">Recommendations</div>`;
    html += recs.length ? recs.map((r, i) => { const a = AGENT(r.agent) || {};
      return `<button class="rec" data-focus="${esc(r.focus)}"><div class="rt"><span class="dot ${r.sev}"></span>${esc(a.icon || "")} ${esc(r.title)}</div><div class="rd">${esc(r.detail)}</div><div class="rby">${esc(a.name || r.agent)}</div></button>`;
    }).join("") : `<div class="empty">No active recommendations in this view.</div>`;
    html += `<div class="sec">Agents</div><div class="roster">`;
    html += agents.filter(a => a.id !== "coordinator").map(a => {
      const n = recs.filter(r => r.agent === a.id).length;
      const open = openAgent === a.id;
      let row = `<button class="ag" data-agent="${a.id}"><span class="ico">${a.icon}</span>${esc(a.name)}${n ? `<span class="badge">${n}</span>` : ""}</button>`;
      if (open) row += `<div class="agdet">${cap("Observes", a.observes)}${cap("Reasons about", a.reasons)}${cap("Can recommend", a.recommend)}${cap("Can act", a.act)}</div>`;
      return row;
    }).join("") + `</div>`;
    html += `<div class="ill">Illustrative — agents surface mock advice from the twin's live state.</div>`;
    menu.innerHTML = html;
    menu.querySelectorAll(".rec").forEach(b => b.onclick = () => onFocus(b.dataset.focus));
    menu.querySelectorAll(".ag").forEach(b => b.onclick = () => { openAgent = openAgent === b.dataset.agent ? null : b.dataset.agent; render(state.get()); });
  }
  const cap = (k, arr) => `<div class="cap"><span>${k}</span> ${esc((arr || []).join(", "))}</div>`;

  state.subscribe(s => { if (!menu.hidden) render(s); });   // keep recommendations live while open
}
