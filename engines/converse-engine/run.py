"""ADR-0111 L4 — the CONVERSE ENGINE (aos SDK, kind:engine).

The channel-agnostic conversational GROUNDING, as a governed component. Behaviourally identical to the host's
former `_converse_core`: read the tenant's governed capability index + experience + publications, ground the
model STRICTLY on them (never invent a figure, service, date, or link), and return {reply, citations[],
suggested[]} with every citation validated against a real record. Every public channel calls this one verb, so
the provenance guarantee lives in one certified place. It reasons; it never writes.
"""
import json
from aos import engine, ctx


def _obj(r):
    return r.get("object") if isinstance(r, dict) and "object" in r else r


def _first_obj(s):
    """The reply is strict JSON, but models wander — pull the first {...} block out, tolerate a ```json fence."""
    s = (s or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[4:] if s[:4].lower() == "json" else s
    i, j = s.find("{"), s.rfind("}")
    try:
        return json.loads(s[i:j + 1]) if i >= 0 and j > i else {}
    except Exception:
        return {}


@engine.verb("converse", implements="platform.converse@0.1.0")
def converse(body):
    """Answer a public question grounded ONLY in the tenant's governed capabilities/experience/publications."""
    tenant = ctx.tenant
    text = str(body.get("text") or body.get("question") or "")[:600]
    locale = body.get("locale") or "en"
    base = str(body.get("base") or "").rstrip("/")

    def mine(schema, lim=200):
        out = []
        try:
            for r in ctx.facts(schema).list():
                o = _obj(r)
                if str(o.get("id", "")).split(":")[1:2] == [tenant]:   # the tenant's OWN records only
                    out.append(o)
        except Exception:
            pass
        return out[:lim]

    prof = (mine("platform.agency_profile", 5) or [{}])[0]
    caps = mine("platform.capability")
    pubs = mine("platform.publication")
    name = prof.get("display_name") or (tenant[:1].upper() + tenant[1:])
    if not caps and not pubs:
        return {"reply": f"{name} has not published any public services yet.", "citations": [], "suggested": [],
                "agency": prof.get("legal_name") or name}

    def open_url(c):
        return (base + "/run?app=" + c.get("target_ref")) if (base and c.get("target_kind") == "app" and c.get("target_ref")) else None

    agency = prof.get("legal_name") or name
    mat = {"agency": agency, "jurisdiction": prof.get("jurisdiction") or "", "mandate": prof.get("mandate") or "",
           "capabilities": [{"ref": c.get("id"), "title": c.get("title"), "type": c.get("type"),
                             "summary": c.get("summary"), "topics": c.get("topics"), "open_url": open_url(c)} for c in caps],
           "notices": [{"ref": p.get("id"), "title": p.get("title"), "summary": p.get("summary"),
                        "status": p.get("status"), "effective_from": p.get("effective_from")} for p in pubs]}
    sysp = (
        f"You are the public assistant for {agency} ({mat['jurisdiction']}). Answer the citizen's question "
        "USING ONLY the AGENCY MATERIAL provided (capabilities = services/datasets the agency offers; notices = "
        "public announcements). Do NOT invent services, figures, dates, or links. If the material does not answer "
        "it, say so plainly and point to what IS available. Be concise (2-4 sentences). "
        f"Write in {'Hindi' if locale == 'hi' else 'English'}. "
        'Return ONLY strict JSON: {"reply": string, "used": [refs you relied on], '
        '"suggested": [up to 3 short follow-up questions the material can answer]}.')
    raw = ctx.ai([{"role": "system", "content": sysp},
                  {"role": "user", "content": "AGENCY MATERIAL:\n" + json.dumps(mat, ensure_ascii=False)
                   + "\n\nQUESTION: " + text}], max_tokens=600)
    out = _first_obj(raw)
    reply = (out.get("reply") or "").strip() or "I don't have that information yet."
    by_ref = {c.get("id"): ("capability", c) for c in caps}
    by_ref.update({p.get("id"): ("publication", p) for p in pubs})
    cites = []
    for ref in (out.get("used") or [])[:6]:
        kc = by_ref.get(ref)
        if not kc:
            continue                                                   # VALIDATE: drop any ref the model invented
        kind, rec = kc
        prov = rec.get("provenance_contract") if isinstance(rec.get("provenance_contract"), str) else None
        cites.append({"ref": ref, "kind": kind, "title": rec.get("title"),
                      "open_url": open_url(rec) if kind == "capability" else None,
                      "provenance": prov or rec.get("source") or ("governed " + kind)})
    sugg = [s for s in (out.get("suggested") or []) if isinstance(s, str)][:3]
    return {"reply": reply, "citations": cites, "suggested": sugg, "agency": agency}


HANDLERS = engine.handlers()
