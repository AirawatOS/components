"""ADR-0111 L4 — the SITE RENDERER engine (aos SDK, kind:engine).

The web presentation adapter as a governed component. Renders the public `/t/<tenant>` home page PURELY from
governed facts: agency_profile + capability index + experience + publications.

ARCHETYPE-AWARE (0.2.0): the network has many participant types — a government agency, a developer, an operator,
a provider, a cross-tenant aggregator, an NGO, the standards body. Each gets a different public presence over the
SAME governed capability index (a dev's capabilities are components; an agency's are services; an aggregator's are
datasets — `capability.type` distinguishes them). `agency_profile.archetype` (default 'government', or derived
from the participant's network type) selects the theme + chrome + how capabilities are grouped/labelled; the
governed data model is unchanged. `agency_profile.primary_color`/`accent_color`/`logo_url` theme it per-participant.

Returns an empty body when the tenant has published no capabilities AND no experience, so the host falls back to
its legacy render for a not-yet-migrated tenant (non-breaking).
"""
import json, html as _h
from aos import engine, ctx

# Per-archetype presentation defaults. The GOVERNMENT archetype is fully realised (the Gov theme); the others
# carry sensible identity + palette defaults so any participant renders coherently, and are refined later.
ARCHETYPES = {
    "government": {"eyebrow": "Official public portal", "primary": "#0b3d6b", "accent": "#b4121b",
                   "cue": "An official portal. Public services and open data, with provenance.",
                   "default_section": "Services & open data"},
    "developer":  {"eyebrow": "Developer on the Airawat Network", "primary": "#1b1f3b", "accent": "#5b8def",
                   "cue": "Published components and documentation.", "default_section": "Published components"},
    "operator":   {"eyebrow": "Certified platform operator", "primary": "#14312a", "accent": "#2fa37c",
                   "cue": "Certified runtimes and operational status.", "default_section": "Runtimes & status"},
    "provider":   {"eyebrow": "Service provider on the Network", "primary": "#1d3a5f", "accent": "#e0872a",
                   "cue": "Services offered to agencies, with terms.", "default_section": "Services offered"},
    "aggregator": {"eyebrow": "Data aggregator", "primary": "#2a2140", "accent": "#8a63d2",
                   "cue": "Aggregated datasets and methodology, with lineage.", "default_section": "Datasets"},
    "ngo":        {"eyebrow": "Civil society organisation", "primary": "#3a1f2b", "accent": "#d2594b",
                   "cue": "Our mission, programmes and open data.", "default_section": "Programmes & data"},
    "network":    {"eyebrow": "Network standards body", "primary": "#111820", "accent": "#5a7fa0",
                   "cue": "The governed catalog, participant directory and governance.", "default_section": "Catalog"},
}
# schema.org @type for the organization, per archetype (GovernmentOrganization only for government).
_ORG_TYPE = {"government": "GovernmentOrganization", "ngo": "NGO"}

_CSS = """
  :root{--bg:#f5f7fa;--card:#ffffff;--ink:#14212e;--soft:#55657a;--line:#e0e6ee;--primary:__PRIMARY__;--accent:__ACCENT__;--on-primary:#ffffff}
  :root:not([data-theme="light"]) @media (prefers-color-scheme:dark){}
  @media (prefers-color-scheme:dark){:root{--bg:#0c1219;--card:#141e29;--ink:#e7edf3;--soft:#9fb0bf;--line:#223243}}
  *{box-sizing:border-box} html{-webkit-text-size-adjust:100%}
  body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);line-height:1.5}
  a{color:inherit}
  .masthead{background:linear-gradient(180deg,var(--primary),color-mix(in srgb,var(--primary) 86%,#000));color:var(--on-primary)}
  .masthead .bar{height:4px;background:var(--accent)}
  .masthead .inner{max-width:1040px;margin:0 auto;padding:20px 24px;display:flex;align-items:center;gap:16px}
  .masthead .logo{height:46px;width:46px;border-radius:8px;object-fit:cover;background:rgba(255,255,255,.12);flex:none}
  .masthead .seal{height:46px;width:46px;border-radius:50%;border:2px solid rgba(255,255,255,.5);display:flex;align-items:center;justify-content:center;font-size:22px;flex:none}
  .masthead .id{flex:1;min-width:0}
  .masthead .eyebrow{font-size:11px;letter-spacing:.09em;text-transform:uppercase;opacity:.82;margin:0 0 3px}
  .masthead h1{margin:0;font-size:22px;font-weight:700;letter-spacing:-.01em}
  .masthead .ju{opacity:.82;font-size:13px;margin-top:3px}
  .masthead .si{background:rgba(255,255,255,.14);color:#fff;text-decoration:none;padding:9px 16px;border-radius:8px;font-size:14px;font-weight:600;border:1px solid rgba(255,255,255,.28);flex:none}
  .masthead .si:hover{background:rgba(255,255,255,.24)}
  .cue{background:color-mix(in srgb,var(--primary) 7%,var(--bg));border-bottom:1px solid var(--line)}
  .cue .inner{max-width:1040px;margin:0 auto;padding:10px 24px;font-size:13px;color:var(--soft);display:flex;align-items:center;gap:8px}
  .cue b{color:var(--ink);font-weight:600}
  .wrap{max-width:1040px;margin:0 auto;padding:30px 24px 64px}
  .mandate{font-size:16px;color:var(--ink);max-width:72ch;margin:0 0 6px} .about{color:var(--soft);font-size:14px;max-width:72ch;margin:0 0 10px}
  h2{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--soft);margin:30px 0 14px;display:flex;align-items:center;gap:10px}
  h2::after{content:"";flex:1;height:1px;background:var(--line)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(248px,1fr));gap:16px}
  .card{display:flex;flex-direction:column;gap:7px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;text-decoration:none;color:var(--ink);transition:border-color .12s,transform .12s,box-shadow .12s}
  .card:hover{border-color:var(--accent);transform:translateY(-2px);box-shadow:0 6px 22px -14px color-mix(in srgb,var(--primary) 60%,transparent)}
  .card .ic{font-size:30px;line-height:1} .card .nm{font-weight:650;font-size:15.5px} .card .sm{color:var(--soft);font-size:13px;line-height:1.45}
  .card .go{color:var(--accent);font-size:13px;font-weight:600;margin-top:auto;padding-top:4px}
  .card:focus-visible,.si:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
  .empty{color:var(--soft);padding:34px 0}
  footer{border-top:1px solid var(--line);margin-top:10px}
  footer .inner{max-width:1040px;margin:0 auto;padding:18px 24px 46px;color:var(--soft);font-size:12.5px;display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center}
  footer a{color:var(--soft);text-decoration:underline;text-underline-offset:2px}
  footer .sp{opacity:.5}
"""


def _obj(r):
    return r.get("object") if isinstance(r, dict) and "object" in r else r


@engine.verb("render-site", implements="platform.render_site@0.1.0")
def render_site(body):
    """Render the public agency home page from governed facts. body: {tenant, path?, base?, locale?}."""
    tenant = ctx.tenant
    base = str(body.get("base") or "").rstrip("/")
    home = base + "/t/" + tenant

    def mine(schema, lim=200):
        out = []
        try:
            for r in ctx.facts(schema).list():
                o = _obj(r)
                if str(o.get("id", "")).split(":")[1:2] == [tenant]:
                    out.append(o)
        except Exception:
            pass
        return out[:lim]

    prof = (mine("platform.agency_profile", 20) or [{}])[0]
    caps = sorted(mine("platform.capability"), key=lambda c: (c.get("type") or "", c.get("title") or ""))
    exps = mine("platform.experience", 10)
    exp = next((e for e in exps if (e.get("status") or "published") == "published" and e.get("sections")), None)
    if not caps and not exp:
        return {}                                              # not migrated → host falls back to legacy SSR

    arch = (prof.get("archetype") or "government").lower()
    A = ARCHETYPES.get(arch, ARCHETYPES["government"])
    name = prof.get("display_name") or (tenant[:1].upper() + tenant[1:])
    legal = prof.get("legal_name") or name
    desc = prof.get("mandate") or A["cue"]
    primary = prof.get("primary_color") or A["primary"]
    accent = prof.get("accent_color") or A["accent"]
    langs = prof.get("languages") or ["en"]

    def open_url(c):
        return (base + "/run?app=" + c.get("target_ref")) if (c.get("target_kind") == "app" and c.get("target_ref")) else None

    app_caps = [c for c in caps if c.get("target_kind") == "app" and c.get("target_ref")]

    # ---- schema.org JSON-LD ----
    org = {"@type": _ORG_TYPE.get(arch, "Organization"), "@id": home + "#org", "name": legal, "url": home, "description": desc}
    for k, v in (("logo", "logo_url"), ("email", "email"), ("telephone", "phone"),
                 ("areaServed", "jurisdiction"), ("address", "address")):
        if prof.get(v):
            org[k] = prof[v]
    if prof.get("social"):
        org["sameAs"] = [s.get("url") for s in prof["social"] if isinstance(s, dict) and s.get("url")]
    graph = [org, {"@type": "WebSite", "@id": home + "#site", "url": home,
                   "name": "%s — %s" % (name, A["default_section"]), "publisher": {"@id": home + "#org"}}]
    svc_items = []
    for i, c in enumerate(app_caps):
        u = open_url(c)
        graph.append({"@type": "GovernmentService" if arch == "government" else "Service", "@id": u + "#svc",
                      "name": c.get("title"), "url": u, "provider": {"@id": home + "#org"},
                      "availableChannel": {"@type": "ServiceChannel", "serviceUrl": u}})
        svc_items.append({"@type": "ListItem", "position": i + 1, "url": u, "name": c.get("title")})
    if svc_items:
        graph.append({"@type": "ItemList", "@id": home + "#services", "name": "%s %s" % (name, A["default_section"]),
                      "itemListElement": svc_items})
    ld = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)

    # ---- body: governed experience sections, else a flat grid ----
    def capcard(c):
        icon = c.get("icon") or "🔎"; t = c.get("title") or ""; s = c.get("summary") or ""; ou = open_url(c)
        inner = ('<div class="ic">%s</div><div class="nm">%s</div><div class="sm">%s</div>'
                 % (_h.escape(icon), _h.escape(t), _h.escape(s)))
        return ('<a class="card" href="%s">%s<div class="go">Open &rarr;</div></a>' % (_h.escape(ou, quote=True), inner)
                if ou else '<div class="card">%s</div>' % inner)

    cap_by_id = {c.get("id"): c for c in caps}
    flat = "".join(capcard(c) for c in caps) or '<div class="empty">Nothing has been published yet.</div>'
    if exp:
        sections = ""
        for sec in (exp.get("sections") or []):
            cards_in = "".join(capcard(cap_by_id[cid]) for cid in (sec.get("capability_ids") or []) if cid in cap_by_id)
            if not cards_in:
                continue
            note = ('<p class="about">%s</p>' % _h.escape(sec.get("note") or "")) if sec.get("note") else ""
            sections += '<h2>%s</h2>%s<div class="grid">%s</div>' % (_h.escape(sec.get("title") or ""), note, cards_in)
        body_html = sections or ('<h2>%s</h2><div class="grid">%s</div>' % (_h.escape(A["default_section"]), flat))
    else:
        body_html = '<h2>%s</h2><div class="grid">%s</div>' % (_h.escape(A["default_section"]), flat)

    title = "%s — %s" % (name, A["default_section"])
    ident = (('<img class="logo" src="%s" alt="">' % _h.escape(prof["logo_url"], quote=True)) if prof.get("logo_url")
             else ('<div class="seal" aria-hidden="true">%s</div>' % _h.escape(prof.get("emblem") or "🏛️")))
    mandate = ('<p class="mandate">%s</p>' % _h.escape(prof.get("mandate"))) if prof.get("mandate") else ""
    about = ('<p class="about">%s</p>' % _h.escape(prof.get("about") or "")) if prof.get("about") else ""
    page = (
        '<!doctype html><html lang="%s"><head><meta charset="utf-8">\n' % _h.escape(langs[0])
        + '<title>%s</title>\n' % _h.escape(title)
        + '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        + '<meta name="description" content="%s">\n' % _h.escape(desc)
        + '<meta name="theme-color" content="%s">\n' % _h.escape(primary)
        + '<link rel="canonical" href="%s">\n' % _h.escape(home, quote=True)
        + '<link rel="sitemap" type="application/xml" href="%s">\n' % _h.escape(home + "/sitemap.xml", quote=True)
        + '<meta property="og:type" content="website"><meta property="og:title" content="%s">\n' % _h.escape(title)
        + '<meta property="og:description" content="%s"><meta property="og:url" content="%s">\n' % (_h.escape(desc), _h.escape(home, quote=True))
        + '<script type="application/ld+json">%s</script>\n' % ld
        + "<style>%s</style></head><body>\n" % _CSS.replace("__PRIMARY__", _h.escape(primary)).replace("__ACCENT__", _h.escape(accent))
        + '<div class="masthead"><div class="bar"></div><div class="inner">%s'
          '<div class="id"><p class="eyebrow">%s</p><h1>%s</h1><div class="ju">%s</div></div>'
          '<a class="si" href="/login">Sign in</a></div></div>\n'
          % (ident, _h.escape(A["eyebrow"]), _h.escape(name), _h.escape(prof.get("jurisdiction") or ""))
        + '<div class="cue"><div class="inner"><b>%s</b></div></div>\n' % _h.escape(A["cue"])
        + '<div class="wrap">\n' + mandate + about + body_html + "\n</div>\n"
        + '<footer><div class="inner"><span>%s</span><span class="sp">·</span><span>open civic data, with provenance</span>'
          '<span class="sp">·</span><a href="%s">sitemap</a><span class="sp">·</span><a href="%s">for AI assistants</a>'
          '<span class="sp">·</span><span>Powered by AirawatOS</span></div></footer>\n'
          % (_h.escape(legal), _h.escape(home + "/sitemap.xml", quote=True), _h.escape(home + "/.well-known/ai-plugin.json", quote=True))
        + '<script src="/shared/concierge.js" defer></script>\n</body></html>')
    return {"content_type": "text/html; charset=utf-8", "body": page}


HANDLERS = engine.handlers()
