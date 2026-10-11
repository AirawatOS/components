#!/usr/bin/env python3
"""ADR-0111 L4 — the Telegram CONVERSATIONAL MESSAGING ADAPTER (kind:adapter), fully CONFINED (clean-room, L1).

Its only I/O is the broker: it READS the agency's governed capability index + experience + publications, calls
the broker MODEL to ground a cited reply, and PUSHES that reply to the citizen via Telegram's API — where the
bot token is a per-tenant Vault secret the BROKER injects into the URL path (`as: template`), so this code never
sees the token. Nothing here is in the host or in compose; the tenant admin sets the token in Settings.

The host transport (/t/<tenant>/telegram/webhook) hands each inbound message to a run of this component as
CHAT_ID + TEXT_B64 (base64 so arbitrary text survives the --set env channel).
"""
import base64, html, json, os
from aos import driver, ctx, egress

BASE = "https://airos.airawat.org"
# Telegram puts the token in the URL PATH (bot<id>:<hash>); the broker substitutes the Vault secret into {TOKEN},
# keeping ':' intact (safe=':'), and this component never sees the value.
INJECT = {"name": "TELEGRAM_BOT_TOKEN", "as": "template", "placeholder": "{TOKEN}", "safe": ":"}
egress("api.telegram.org", rate_per_min=30,
       user_agent="AirawatOS/telegram-concierge (+https://airawat.org)", secret="TELEGRAM_BOT_TOKEN")


def _open(c):
    return (BASE + "/run?app=" + c.get("target_ref")) if c.get("target_kind") == "app" and c.get("target_ref") else None


def _json(s):
    s = (s or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[4:] if s[:4].lower() == "json" else s
    i, j = s.find("{"), s.rfind("}")
    try:
        return json.loads(s[i:j + 1]) if i >= 0 and j > i else {}
    except Exception:
        return {}


@driver.main
def run(ctx):
    tenant = ctx.tenant
    chat_id = os.environ.get("CHAT_ID", "").strip()
    try:
        text = base64.b64decode(os.environ.get("TEXT_B64", "")).decode("utf-8", "replace")[:600].strip()
    except Exception:
        text = ""
    if not chat_id or not text:
        print("telegram-concierge: no CHAT_ID/TEXT — nothing to do")
        return

    def mine(scope):
        out = []
        for r in ctx.facts(scope).list():
            o = r.get("object") or r
            if str(o.get("id", "")).split(":")[1:2] == [tenant]:
                out.append(o)
        return out

    prof = (mine("platform.agency_profile") or [{}])[0]
    caps = mine("platform.capability")
    pubs = mine("platform.publication")
    name = prof.get("display_name") or tenant
    agency = prof.get("legal_name") or name
    cites = []   # list of (kind, record)

    if not caps and not pubs:
        reply = f"{name} has not published any public services yet."
    else:
        mat = {"agency": agency, "jurisdiction": prof.get("jurisdiction") or "",
               "capabilities": [{"ref": c.get("id"), "title": c.get("title"), "type": c.get("type"),
                                 "summary": c.get("summary"), "topics": c.get("topics"), "open_url": _open(c)} for c in caps],
               "notices": [{"ref": p.get("id"), "title": p.get("title"), "summary": p.get("summary"),
                            "status": p.get("status")} for p in pubs]}
        sysp = (f"You are the public assistant for {agency} ({mat['jurisdiction']}). Answer the citizen's question "
                "USING ONLY the AGENCY MATERIAL (capabilities = services/datasets; notices = announcements). Do NOT "
                "invent services, figures, dates, or links. If it is not covered, say so and point to what IS "
                'available. Be concise (2-4 sentences). Return ONLY JSON: {"reply": string, "used": [refs]}.')
        by = {c.get("id"): ("capability", c) for c in caps}
        by.update({p.get("id"): ("publication", p) for p in pubs})
        try:
            raw = ctx.ai([{"role": "system", "content": sysp},
                          {"role": "user", "content": "AGENCY MATERIAL:\n" + json.dumps(mat, ensure_ascii=False)
                           + "\n\nQUESTION: " + text}], max_tokens=600)
            out = _json(raw)
            reply = (out.get("reply") or "").strip()
            for ref in (out.get("used") or [])[:6]:
                if ref in by:
                    cites.append(by[ref])
        except Exception as e:
            print(f"telegram-concierge: model unavailable ({str(e)[:80]}) — listing capabilities verbatim")
            reply = ""
        if not reply:   # grounded fallback: never fabricate — just name what the agency offers, from the records
            reply = f"{agency} offers: " + "; ".join(c.get("title", "") for c in caps[:6]) + "."
            cites = [("capability", c) for c in caps[:4]]

    parts = [html.escape(reply)]
    for kind, rec in cites:
        t = html.escape(rec.get("title") or "")
        ou = _open(rec) if kind == "capability" else None
        parts.append(f'🔗 <a href="{html.escape(ou)}">{t}</a>' if ou else (("📢 " if kind == "publication" else "🔗 ") + t))
    parts.append(f'<i>— {html.escape(agency)} · governed public data</i>')
    msg = "\n\n".join(p for p in parts if p)[:4000]

    # OUTBOUND via brokered egress — the broker injects TELEGRAM_BOT_TOKEN into {TOKEN} server-side (prov.egress
    # is audited, the value redacted); the adapter never holds the token.
    body = {"chat_id": chat_id, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": False}
    r = ctx.fetch("https://api.telegram.org/bot{TOKEN}/sendMessage", method="POST", body=body, inject=INJECT)
    print(f"telegram-concierge: tenant={tenant} chat={chat_id} reply={len(reply)}c cites={len(cites)} "
          f"send_status={getattr(r, 'status', None)}")
