"""ADR-0111 L4 — the SITE RENDERER engine (aos SDK, kind:engine).

The web presentation adapter as a governed component. Behaviourally the hardcoded host /t/<tenant> home SSR,
rebuilt to render PURELY from governed facts: the tenant's agency_profile + capability index + experience +
publications. The public "apps" grid is sourced from app-type CAPABILITIES (target_kind==app) — the governed
discovery index — not from host process state, so the renderer is a clean read-only component. Returns the full
HTML page (SEO head + schema.org JSON-LD). Returns an empty body when the tenant has published no capabilities
AND no experience, so the host falls back to its legacy render for a not-yet-migrated tenant (non-breaking).
"""
import json, html as _h
from aos import engine, ctx

_CSS = """
  :root{--bg:#f4f6f9;--ink:#15212e;--soft:#5a6b7b;--line:#dde4ec;--primary:%(primary)s}
  @media (prefers-color-scheme:dark){:root{--bg:#0d141c;--ink:#e7edf3;--soft:#9fb0bf;--line:#243444}}
  *{box-sizing:border-box} body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
  header{background:var(--primary);color:#fff;padding:22px 24px;display:flex;align-items:center;gap:14px}
  header .id{flex:1} header h1{margin:0;font-size:20px} header .ju{opacity:.8;font-size:12px;margin-top:2px}
  header .si{background:#1d60a8;color:#fff;text-decoration:none;padding:9px 16px;border-radius:8px;font-size:14px;font-weight:600}
  .wrap{max-width:980px;margin:0 auto;padding:26px 24px 60px}
  .tag{color:var(--soft);font-size:13px;margin:0 0 6px} .about{color:var(--soft);font-size:13px;margin:0 0 22px;max-width:70ch}
  h2{font-size:14px;text-transform:uppercase;letter-spacing:.05em;color:var(--soft);margin:24px 0 12px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:16px}
  .card{display:flex;flex-direction:column;gap:6px;background:#fff;border:1px solid var(--line);border-radius:12px;padding:18px;text-decoration:none;color:var(--ink)}
  @media (prefers-color-scheme:dark){.card{background:#141e29}}
  .card:hover{border-color:#1d60a8} .card .ic{font-size:30px} .card .nm{font-weight:600;font-size:15px} .card .sm{color:var(--soft);font-size:12.5px;line-height:1.4} .card .go{color:#1d60a8;font-size:13px;margin-top:4px}
  .empty{color:var(--soft);padding:30px 0}
  footer{color:var(--soft);font-size:12px;max-width:980px;margin:0 auto;padding:0 24px 40px;border-top:1px solid var(--line);padding-top:16px}
  footer a{color:var(--soft)}
"""


def _obj(r):
    return r.get("object") if isinstance(r, dict) and "object" in r else r


@engine.verb("render-site", implements="platform.render_site@0.1.0")
def render_site(body):
    """Render the public agency home page from governed facts. body: {tenant, path?, base?}."""
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

    name = prof.get("display_name") or (tenant[:1].upper() + tenant[1:])
    legal = prof.get("legal_name") or name
    desc = prof.get("mandate") or ("Public services and open data from %s." % name)
    primary = prof.get("primary_color") or "#0e2a47"
    langs = prof.get("languages") or ["en"]

    def open_url(c):
        return (base + "/run?app=" + c.get("target_ref")) if (c.get("target_kind") == "app" and c.get("target_ref")) else None

    # the public "apps" = app-type capabilities (the governed discovery index)
    app_caps = [c for c in caps if c.get("target_kind") == "app" and c.get("target_ref")]

    # ---- schema.org JSON-LD: Organization + WebSite + GovernmentService/Dataset per app capability + ItemList ----
    org = {"@type": "GovernmentOrganization", "@id": home + "#org", "name": legal, "url": home, "description": desc}
    for k, v in (("logo", "logo_url"), ("email", "email"), ("telephone", "phone"),
                 ("areaServed", "jurisdiction"), ("address", "address")):
        if prof.get(v):
            org[k] = prof[v]
    if prof.get("social"):
        org["sameAs"] = [s.get("url") for s in prof["social"] if isinstance(s, dict) and s.get("url")]
    graph = [org, {"@type": "WebSite", "@id": home + "#site", "url": home,
                   "name": "%s — Public Services" % name, "publisher": {"@id": home + "#org"}}]
    svc_items = []
    for i, c in enumerate(app_caps):
        u = open_url(c)
        graph.append({"@type": "GovernmentService", "@id": u + "#svc", "name": c.get("title"), "url": u,
                      "serviceType": "Civic information service", "provider": {"@id": home + "#org"},
                      "availableChannel": {"@type": "ServiceChannel", "serviceUrl": u}})
        svc_items.append({"@type": "ListItem", "position": i + 1, "url": u, "name": c.get("title")})
    if svc_items:
        graph.append({"@type": "ItemList", "@id": home + "#services", "name": "%s public services" % name,
                      "itemListElement": svc_items})
    ld = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)

    # ---- body: the governed experience sections (capability cards), or a flat grid of all capabilities ----
    def capcard(c):
        icon = c.get("icon") or "🔎"; t = c.get("title") or ""; s = c.get("summary") or ""; ou = open_url(c)
        inner = ('<div class="ic">%s</div><div class="nm">%s</div><div class="sm">%s</div>'
                 % (_h.escape(icon), _h.escape(t), _h.escape(s)))
        return ('<a class="card" href="%s">%s<div class="go">Open &rarr;</div></a>' % (_h.escape(ou, quote=True), inner)
                if ou else '<div class="card">%s</div>' % inner)

    cap_by_id = {c.get("id"): c for c in caps}
    flat = "".join(capcard(c) for c in caps) or '<div class="empty">No public services have been published yet.</div>'
    if exp:
        sections = ""
        for sec in (exp.get("sections") or []):
            cards_in = "".join(capcard(cap_by_id[cid]) for cid in (sec.get("capability_ids") or []) if cid in cap_by_id)
            if not cards_in:
                continue
            note = ('<p class="about">%s</p>' % _h.escape(sec.get("note") or "")) if sec.get("note") else ""
            sections += '<h2>%s</h2>%s<div class="grid">%s</div>' % (_h.escape(sec.get("title") or ""), note, cards_in)
        body_html = sections or ('<h2>Services &amp; open data</h2><div class="grid">%s</div>' % flat)
    else:
        body_html = '<h2>Services &amp; open data</h2><div class="grid">%s</div>' % flat

    title = "%s — Public Services" % name
    logo_html = ('<img src="%s" alt="" style="height:38px;border-radius:6px">' % _h.escape(prof["logo_url"], quote=True)) if prof.get("logo_url") else ""
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
        + "<style>%s</style></head><body>\n" % (_CSS % {"primary": _h.escape(primary)})
        + '<header>%s<div class="id"><h1>%s</h1><div class="ju">%s</div></div>\n' % (logo_html, _h.escape(name), _h.escape(prof.get("jurisdiction") or ""))
        + '<a class="si" href="/login">Sign in</a></header>\n<div class="wrap">\n'
        + '<p class="tag">Public services — no sign-in required. Staff and registered users can sign in for more.</p>\n'
        + about + body_html + "\n</div>\n"
        + '<footer>%s · open civic data, with provenance · <a href="%s">sitemap</a> · <a href="%s">for AI assistants</a> · Powered by AirawatOS</footer>\n'
          % (_h.escape(legal), _h.escape(home + "/sitemap.xml", quote=True), _h.escape(home + "/.well-known/ai-plugin.json", quote=True))
        + '<script src="/shared/concierge.js" defer></script>\n</body></html>')
    return {"content_type": "text/html; charset=utf-8", "body": page}


HANDLERS = engine.handlers()
