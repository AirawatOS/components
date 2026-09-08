// twin/ui.js — the digital-twin shell: breadcrumb (TL), lifecycle selector (TC), lens rail (L), inspector (R).
// Pure function of DigitalTwinState — subscribes and re-renders. Emits intent via callbacks; never mutates Cesium.

const LIFECYCLES = [["plan", "PLAN"], ["build", "BUILD"], ["operate", "OPERATE"]];
const LENSES = [["physical", "Physical", "◧"], ["compute", "Compute", "▦"], ["digital", "Digital", "◇"], ["innovation", "Innovation", "✦"], ["governance", "Governance", "⚖"]];
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const $ = id => document.getElementById(id);

export function mountShell({ state, repo, onNavigate, onEnterBuilding, networks, lenses, buildInspector, operateInspector }) {
  const LENSMODS = lenses || {};   // { compute: {inspector}, digital: {inspector}, … }
  const BUILDINSP = buildInspector, OPINSP = operateInspector;
  // ---- Overlays popover: networks (+ scenario section filled by app.js) ----
  const ovbtn = $("dt-ovbtn"), ovmenu = $("dt-ovmenu");
  if (ovbtn && ovmenu) {
    ovbtn.onclick = e => { e.stopPropagation(); const open = ovmenu.hidden; ovmenu.hidden = !open; ovbtn.classList.toggle("on", open); ovbtn.setAttribute("aria-expanded", open ? "true" : "false"); };
    document.addEventListener("click", e => { if (!ovmenu.hidden && !ovmenu.contains(e.target) && e.target !== ovbtn) { ovmenu.hidden = true; ovbtn.classList.remove("on"); ovbtn.setAttribute("aria-expanded", "false"); } });
  }
  const nets = networks || [];
  const PRESETS = { Utilities: ["power", "water"], Digital: ["fiber"], All: nets.map(n => n.kind) };
  const nx = $("ov-networks");
  if (nx && nets.length) {
    nx.innerHTML = nets.map(n => `<button data-net="${n.kind}"><span class="sw" style="background:rgb(${n.color.join(",")})"></span>${esc(n.name)}</button>`).join("")
      + Object.keys(PRESETS).map(p => `<button class="clr" data-preset="${p}">${p}</button>`).join("")
      + `<button class="clr" data-preset="__off">Off</button>`;
    nx.querySelectorAll("[data-net]").forEach(b => b.onclick = () => {
      const cur = state.get().activeNetworks.slice(), k = b.dataset.net, i = cur.indexOf(k);
      i >= 0 ? cur.splice(i, 1) : cur.push(k); state.patch({ activeNetworks: cur });
    });
    nx.querySelectorAll("[data-preset]").forEach(b => b.onclick = () => {
      const p = b.dataset.preset; state.patch({ activeNetworks: p === "__off" ? [] : (PRESETS[p] || []).slice() });
    });
  }

  // ---- lifecycle (top-centre) ----
  const lc = $("dt-lifecycle");
  lc.innerHTML = LIFECYCLES.map(([k, l]) => `<button data-lc="${k}">${l}</button>`).join("");
  lc.querySelectorAll("button").forEach(b => b.onclick = () => state.patch({ lifecycleMode: b.dataset.lc }));

  // ---- lens rail (left) ----
  const lr = $("dt-lens");
  lr.innerHTML = LENSES.map(([k, l, i]) => `<button data-lens="${k}" title="${l}"><span class="i">${i}</span><span class="l">${l}</span></button>`).join("");
  lr.querySelectorAll("button").forEach(b => b.onclick = () => state.patch({ lens: b.dataset.lens }));

  // ---- react to state ----
  const render = s => {
    lc.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.lc === s.lifecycleMode));
    lr.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.lens === s.lens));
    if (nx) nx.querySelectorAll("[data-net]").forEach(b => b.classList.toggle("on", s.activeNetworks.indexOf(b.dataset.net) >= 0));
    renderBreadcrumb(s);
    renderInspector(s);
  };

  function renderBreadcrumb(s) {
    const el = $("dt-breadcrumb");
    const chain = s.breadcrumb.map(repo.get).filter(Boolean);
    el.classList.toggle("show", chain.length > 1);   // hide at city level (the cap title already says "AI City")
    el.innerHTML = chain.map((e, i) =>
      `<span class="crumb${i === chain.length - 1 ? " cur" : ""}" data-id="${e.id}">${esc(e.name)}</span>`
    ).join('<span class="sep">›</span>');
    el.querySelectorAll(".crumb").forEach(c => c.onclick = () => onNavigate(c.dataset.id));
  }

  function renderInspector(s) {
    const el = $("dt-inspector");
    // BUILD lifecycle overrides the inspector for a selected building (construction status), across any lens
    if (s.lifecycleMode === "build" && BUILDINSP && s.selectedEntityId) {
      const be = repo.get(s.selectedEntityId);
      if (be && be.type === "building") { const r = BUILDINSP(be, s); el.innerHTML = card(r.title, r.body, "build"); el.hidden = false; return; }
    }
    // OPERATE lifecycle → live telemetry for a selected building or network node
    if (s.lifecycleMode === "operate" && OPINSP && s.selectedEntityId) {
      const oe = repo.get(s.selectedEntityId);
      if (oe && (oe.type === "building" || oe.type === "net-node")) { const r = OPINSP(oe, s); el.innerHTML = card(r.title, r.body, "operate"); el.hidden = false; return; }
    }
    if (s.lens !== "physical") {
      const lm = LENSMODS[s.lens];
      if (lm && lm.inspector) {                                    // a wired lens (compute / digital / …)
        const e = s.selectedEntityId ? repo.get(s.selectedEntityId) : null;
        const r = lm.inspector(e, repo);
        el.innerHTML = card(r.title, r.body, s.lens); el.hidden = false;
        el.querySelectorAll(".li").forEach(b => b.onclick = () => onNavigate(b.dataset.id));
        return;
      }
      el.innerHTML = card((LENSES.find(l => l[0] === s.lens) || [0, s.lens])[1] + " lens",  // still-stubbed lens
        `<div class="dim">This lens arrives in a later phase. The digital-twin model, selection and drilldown are shared across all lenses.</div>`);
      el.hidden = false; return;
    }
    const e = s.selectedEntityId ? repo.get(s.selectedEntityId) : repo.getRoot();
    if (!e) { el.hidden = true; return; }
    const kids = repo.getChildren(e.id);
    const rels = repo.getRelationships(e.id);
    let body = `<div class="row"><span>Type</span><b>${esc(e.type)}</b></div>`;
    if (e.function) body += `<div class="row"><span>Function</span><b>${esc(e.function)}</b></div>`;
    if (e.metadata && e.metadata.heightM) body += `<div class="row"><span>Height</span><b>${e.metadata.heightM} m</b></div>`;
    if (e.metadata && e.metadata.floors) body += `<div class="row"><span>Floors</span><b>${e.metadata.floors}</b></div>`;
    if (e.lifecycle && e.lifecycle.status) body += `<div class="row"><span>Status</span><b>${esc(e.lifecycle.status)}</b></div>`;
    if (e.type === "building") body += `<button class="enter" id="dt-enter">Enter building →</button>`;
    if (kids.length) body += `<div class="sub">${e.type === "city" ? "Zones" : e.type === "zone" ? "Parcels" : e.type === "parcel" ? "Buildings" : "Contains"} (${kids.length})</div>`
      + `<div class="list">` + kids.slice(0, 60).map(k => `<button class="li" data-id="${k.id}">${esc(k.name)}</button>`).join("") + `</div>`;
    if (rels.length) body += `<div class="sub">Relationships</div><div class="rels">`
      + rels.slice(0, 12).map(r => `<div class="rel">${esc(r.sourceId === e.id ? r.relationshipType : "← " + r.relationshipType)} <b>${esc(r.sourceId === e.id ? r.targetId : r.sourceId)}</b></div>`).join("") + `</div>`;
    body += `<div class="ill">Illustrative demo data</div>`;
    el.innerHTML = card(e.name, body, e.type);
    el.hidden = false;
    const enter = $("dt-enter"); if (enter) enter.onclick = () => onEnterBuilding(e.id);
    el.querySelectorAll(".li").forEach(b => b.onclick = () => onNavigate(b.dataset.id));
  }

  const card = (title, body, kind) => `<div class="dt-card"><div class="hd"><span class="k">${esc(kind || "")}</span><div class="nm">${esc(title)}</div></div>${body}</div>`;

  const unsub = state.subscribe(render);
  render(state.get());
  return { render, dispose: unsub };
}
