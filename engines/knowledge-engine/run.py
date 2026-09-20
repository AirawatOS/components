#!/usr/bin/env python3
"""Knowledge Engine — the grounded-RAG brain, extracted out of app_host (`_bridge_expert_chat` + `_bridge_retrieve`).

Two handlers, both GROUNDED (answers/results built only from the tenant's own governed facts, never free-invention):
  • expert-chat        — chat with an installed AI Expert: assemble grounding (the Expert's own recommendations +
                         governance rules in its domain + cited knowledge retrieved for the question + the tenant's
                         OWN live domain data) → prompt the model → return {answer, model, expert, sources}.
  • knowledge-retrieve — semantic search: embed the query, cosine-rank the tenant's knowledge.embedding vectors,
                         return top-k chunks each wrapped as a verifiable CITATION.

Reads the registry AS THE USER (ctx.token) so results are tenant-scoped (fail-closed on no session). Uses host-held
ctx capabilities: `embed` (embeddings gateway), `http` (model /complete + network store), `kernel_url`/`network_url`.
Pure knowledge logic; no per-tenant secrets. DOMAIN_DATA is the engine's domain-summary config (a later slice can
make it governed data).
"""
import json as _json, time as _t
from collections import Counter as _Counter

# Which live domain schemas the Expert summarises, by domain token in its id (flood-expert → "flood"). GOVERNED-first:
# read a ref.codelist/knowledge-domain-data whose items are "domain=schema" pairs; fall back to this default.
DEFAULT_DOMAIN_DATA = {"flood": ["risk.flood"], "water": ["risk.flood"]}


def _domain_map(ctx):
    try:
        for oid in (f"ds:{ctx.get('tenant')}:ref/codelist/knowledge-domain-data",
                    "ds:airawat:ref/codelist/knowledge-domain-data"):
            r = ctx["http"]("POST", ctx["kernel_url"] + "/v1/registry/resolve", {"id": oid}, ctx.get("token"))
            rs = (r or {}).get("results", [])
            items = (rs[0]["object"] if rs else {}).get("items")
            if isinstance(items, list) and items:
                m = {}
                for it in items:
                    if isinstance(it, str) and "=" in it:
                        d, s = it.split("=", 1); m.setdefault(d.strip(), []).append(s.strip())
                if m:
                    return m
    except Exception:
        pass
    return DEFAULT_DOMAIN_DATA


def _read(ctx, schema, limit):
    """Plain USER-scoped registry read (ctx.token) — matches app_host's `_resolve` with the user token, so the
    kernel tenant/RBAC-gates it exactly like the signed-in user's own read (not engine-attributed)."""
    try:
        r = ctx["http"]("POST", ctx["kernel_url"] + "/v1/registry/resolve",
                        {"filter": {"schema": schema}, "limit": limit}, ctx.get("token"))
        return [x["object"] for x in (r or {}).get("results", [])]
    except Exception:
        return []


def _flood_summary(ctx, rows):
    """Port of the risk.flood live-data summary line (levels + worst score + population-in-hotspots + model tier)."""
    _lv = _Counter(o.get("level") for o in rows)
    _sc = [o.get("score") for o in rows if isinstance(o.get("score"), (int, float))]
    _mx = round(max(_sc), 3) if _sc else None
    _hot = {o.get("h3_id") for o in rows if o.get("level") in ("high", "severe")}
    _r0 = rows[0].get("forecast") or rows[0].get("drivers", {})
    _fc = _r0.get("rainfall_mm") if isinstance(_r0, dict) else None
    _ppl = None
    _pop = _read(ctx, "demographics.population", 20000)
    if _pop:
        _ppl = round(sum((o.get("population") or 0) for o in _pop if ((o.get("space") or {}).get("h3")) in _hot))
    _tier = _Counter((o.get("drivers") or {}).get("tier", "T1") for o in rows)
    _t2 = _tier.get("T2", 0); _t1 = _tier.get("T1", 0)
    _method = ("terrain × rainfall forecast, with OSM-drainage relief where drains are mapped (tier T2)"
               if _t2 else "terrain × rainfall forecast only (tier T1 — no drainage data yet)")
    return (f"Predicted flood risk for this city (risk.flood, model): {len(rows)} cells — "
            f"severe {_lv.get('severe',0)}, high {_lv.get('high',0)}, moderate {_lv.get('moderate',0)}, low {_lv.get('low',0)}; "
            f"worst-cell ponding-risk score {_mx} (0-1). Method: {_method}; "
            f"{_t2} cells got drainage relief (T2), {_t1} are terrain-only (T1). "
            f"A cell's risk rises with low-lying terrain + heavy rainfall and FALLS where dense drainage conveys runoff."
            + (f" Forecast scenario ~{_fc}mm/24h." if _fc else "")
            + (f" Est. resident population in high/severe-risk cells: ~{_ppl:,}." if _ppl else ""))


def _knowledge_hits(ctx, query, k):
    """Embed the query + cosine-rank the tenant's knowledge.embedding → [(chunk, doc)] hits (shared by both handlers)."""
    er = ctx["embed"]([query], "query")
    model, qv = er["model"], er["vectors"][0]
    embs = [o for o in _read(ctx, "knowledge.embedding", 5000) if o.get("model") == model and o.get("vector")]
    chunks = {c["chunk_hash"]: c for c in _read(ctx, "knowledge.chunk", 3000) if c.get("chunk_hash")}
    docs = {d.get("doc_id"): d for d in _read(ctx, "knowledge.document", 500)}
    cos = lambda a, b: sum(x * y for x, y in zip(a, b))
    out = []
    for score, e in sorted(((cos(qv, e["vector"]), e) for e in embs), key=lambda t: -t[0])[:k]:
        ch = chunks.get(e["chunk_hash"])
        if not ch:
            continue
        out.append((score, ch, docs.get(ch.get("doc_id"), {})))
    return model, out


def h_expert_chat(ctx):
    b = ctx["body"]
    expert_id = b.get("expert_id", ""); message = (b.get("message") or "").strip()
    history = b.get("history") or []
    if not message:
        return None, "empty message"

    # 1) the Expert — persona + member skills — from the store listing
    name, desc, members = "AI Expert", "", []
    try:
        lst = (ctx["http"]("POST", ctx["network_url"] + "/v1/store/resolve", {"component_id": expert_id}) or {}).get("listing") or {}
        name = lst.get("title") or name; desc = lst.get("description") or ""; members = lst.get("members") or []
    except Exception:
        pass
    # 2) the Expert's OWN recommendations (latest first)
    recs = _read(ctx, "governance.recommendation", 200)
    mine = [r for r in recs if (not members) or (r.get("skill") in members)] or recs
    mine = sorted(mine, key=lambda r: r.get("created_at", ""), reverse=True)[:4]
    # 3) governance rules in force, preferring THIS expert's domain
    allrules = _read(ctx, "governance.rule", 300)
    _all = [r for r in allrules if r.get("status") != "proposed"]
    _dom = [t for t in expert_id.rsplit("/", 1)[-1].replace("-expert", "").replace("-", " ").split() if t]
    def _rel(r):
        hay = (str(r.get("applies_to", "")) + " " + str(r.get("subject", "")) + " "
               + str((r.get("source", {}) or {}).get("doc", "")) + " " + str(r.get("title", ""))).lower()
        return any(t in hay for t in _dom)
    approved = [r for r in _all if _rel(r)][:15] or _all[:12]
    # 4) cited knowledge for the question
    kn = []
    try:
        _model, hits = _knowledge_hits(ctx, message, 4)
        for score, ch, d in hits:
            kn.append({"text": ch.get("text"), "chunk_hash": ch["chunk_hash"],
                       "citation": {"title": d.get("title"), "locator": ch.get("locator"), "authority": d.get("issuer")}})
    except Exception:
        kn = []
    # ---- assemble the grounded prompt ----
    ctxl = []
    if mine:
        ctxl.append("## Your recent recommendations")
        for r in mine:
            ff = r.get("for_facts") or {}; cit = r.get("citation") or {}
            ctxl.append(f"- {r.get('title')}: {r.get('summary')} (action: {r.get('proposed_action')}; "
                        f"rule {cit.get('rule')}, {cit.get('authority')}"
                        + (f"; measured mean {ff.get('mean')}, max {ff.get('max')} over {ff.get('n')} cells" if ff.get('mean') is not None else "") + ")")
    if approved:
        ctxl.append("## Governance rules in force")
        for r in approved:
            q = (f"{r.get('metric')} {r.get('operator')} {r.get('value')} {r.get('unit') or ''}".strip()
                 if r.get("operator") else (r.get("statement") or r.get("proposed_action") or ""))
            ctxl.append(f"- [{r.get('stage') or r.get('subject') or r.get('rule_id')}] {q} — {r.get('binding')}, {r.get('authority')}")
    if kn:
        ctxl.append("## Knowledge excerpts (cite as [n])")
        for i, c in enumerate(kn, 1):
            ct = c.get("citation") or {}
            ctxl.append(f"[{i}] ({ct.get('title')}, {(ct.get('locator') or {}).get('label')}) {(c.get('text') or '')[:500]}")
    # ---- LIVE DATA grounding: the tenant's OWN current facts for this domain (governed domain→schema map) ----
    data_lines = []
    _ddata = _domain_map(ctx)
    for _t0 in _dom:
        for _sch in _ddata.get(_t0, []):
            rows = _read(ctx, _sch, 20000)
            if rows and _sch == "risk.flood":
                data_lines.append(_flood_summary(ctx, rows))
    if data_lines:
        ctxl.append("## Current data for this city (its OWN live facts — use as the situational picture)")
        ctxl.extend("- " + _l for _l in data_lines)
    context = "\n".join(ctxl) or "(no grounded data is available yet for this domain)"
    system = (f"You are {name}, an AI Expert on the AirawatOS platform. {desc}\n"
              "Answer the user's question using ONLY the CONTEXT below — your recommendations, the governance "
              "rules in force, and the cited knowledge excerpts. Cite the rule, authority, or document [n] you "
              "rely on. If the answer is not in the context, say you do not have that data rather than guessing. "
              "NEVER invent a number, place, or fact that is not in the context. You propose and inform; you do "
              "not decide. Be concise and practical. Respond with ONLY the final answer addressed to the user — "
              "do NOT show your reasoning, analysis, planning, or meta-commentary (no \"Let me…\", no \"The user "
              "is asking…\"); go straight to the answer.\n\nCONTEXT:\n") + context
    msgs = [{"role": "system", "content": system}]
    for h in history[-6:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": message})
    res = None; last = None
    for _a in range(3):
        try:
            res = ctx["http"]("POST", ctx["embed_url"] + "/complete", {"messages": msgs}); break
        except Exception as e:
            last = e; _t.sleep(1.0 * (_a + 1))
    if res is None:
        return None, "the expert's model is unavailable right now: " + str(last)
    sources = ([{"type": "recommendation", "id": r.get("id"), "title": r.get("title")} for r in mine]
               + [{"type": "knowledge", "n": i, "title": (c.get("citation") or {}).get("title"),
                   "chunk_hash": c.get("chunk_hash"), "locator": (c.get("citation") or {}).get("locator")}
                  for i, c in enumerate(kn, 1)])
    return {"answer": (res.get("content") or "").strip(), "model": res.get("model"), "expert": name, "sources": sources}, None


def h_knowledge_retrieve(ctx):
    b = ctx["body"]
    query = b.get("query", ""); k = int(b.get("k", 5))
    try:
        model, hits = _knowledge_hits(ctx, query, k)
    except Exception as e:
        return None, str(e)
    out = [{"score": round(score, 4), "text": ch.get("text"), "chunk_hash": ch["chunk_hash"],
            "citation": {"doc_id": ch.get("doc_id"), "title": doc.get("title"), "version": doc.get("current_version"),
                         "locator": ch.get("locator"), "legal_force": doc.get("legal_force"), "authority": doc.get("issuer")}}
           for score, ch, doc in hits]
    return {"model": model, "query": query, "results": out}, None


HANDLERS = {"expert-chat": h_expert_chat, "knowledge-retrieve": h_knowledge_retrieve}


def main():
    print("Knowledge Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
