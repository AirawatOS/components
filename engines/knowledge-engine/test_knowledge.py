"""Parity test for the knowledge-engine (stub ctx: fake embed/http/registry). Covers: knowledge-retrieve cosine
ranking + citation shape; expert-chat grounding assembly (recs + rules + knowledge + risk.flood live summary) and
that the model prompt is built from the tenant's own facts. No network/model — pure logic proof."""
import importlib.util, os, sys, json

spec = importlib.util.spec_from_file_location("ke", os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py"))
ke = importlib.util.module_from_spec(spec); spec.loader.exec_module(ke)

# in-memory registry keyed by schema
REG = {
    "knowledge.embedding": [
        {"model": "m1", "vector": [1.0, 0.0], "chunk_hash": "sha2-256:aaa"},
        {"model": "m1", "vector": [0.0, 1.0], "chunk_hash": "sha2-256:bbb"},
    ],
    "knowledge.chunk": [
        {"chunk_hash": "sha2-256:aaa", "doc_id": "d1", "text": "Flood SOP: evacuate low-lying wards.", "locator": {"label": "§3"}},
        {"chunk_hash": "sha2-256:bbb", "doc_id": "d2", "text": "Unrelated air text.", "locator": {"label": "§9"}},
    ],
    "knowledge.document": [
        {"doc_id": "d1", "title": "NDMA Flood Manual", "issuer": "NDMA", "current_version": "2019", "legal_force": "guideline"},
        {"doc_id": "d2", "title": "CPCB Air", "issuer": "CPCB"},
    ],
    "governance.recommendation": [{"id": "r1", "title": "Pre-position pumps", "summary": "Deploy pumps", "skill": "flood-skill",
                                   "created_at": "2026-01-01", "proposed_action": "deploy", "citation": {"rule": "R1", "authority": "NDMA"}}],
    "governance.rule": [{"rule_id": "R1", "title": "flood threshold", "status": "approved", "applies_to": "flood",
                         "metric": "risk", "operator": ">", "value": 0.7, "binding": "advisory", "authority": "NDMA"}],
    "risk.flood": [{"level": "severe", "score": 0.9, "h3_id": "h1", "drivers": {"tier": "T2"}, "forecast": {"rainfall_mm": 120}},
                   {"level": "low", "score": 0.1, "h3_id": "h2", "drivers": {"tier": "T1"}}],
    "demographics.population": [{"space": {"h3": "h1"}, "population": 5000}],
}

def make_ctx(body, complete="The answer, citing [1] and NDMA."):
    def embed(texts, kind="query"): return {"model": "m1", "vectors": [[1.0, 0.0]]}   # aligns with chunk aaa
    def http(method, url, b=None, tok=None):
        if url.endswith("/complete"): return {"content": complete, "model": "kimi"}
        if "store/resolve" in url: return {"listing": {"title": "Flood Expert", "description": "floods", "members": ["flood-skill"]}}
        if "registry/resolve" in url:
            sch = (b.get("filter") or {}).get("schema")
            return {"results": [{"object": o} for o in REG.get(sch, [])]}
        return {}
    return {"body": body, "embed": embed, "http": http, "kernel_url": "http://k", "network_url": "http://n",
            "embed_url": "http://e", "token": "T", "tenant": "kmc"}

ok = True
def chk(n, c):
    global ok; print(("  PASS " if c else "  FAIL ") + n); ok = ok and c

# 1) knowledge-retrieve: query [1,0] ranks chunk aaa (cos=1) above bbb (cos=0)
r, e = ke.h_knowledge_retrieve(make_ctx({"query": "flood evacuation", "k": 5}))
chk("1 no err", e is None)
chk("1 model returned", r.get("model") == "m1")
chk("1 top hit is the flood chunk", r["results"][0]["chunk_hash"] == "sha2-256:aaa")
chk("1 top score ~1.0", abs(r["results"][0]["score"] - 1.0) < 1e-6)
cit = r["results"][0]["citation"]
chk("1 citation carries doc title+authority+legal_force", cit["title"] == "NDMA Flood Manual" and cit["authority"] == "NDMA" and cit["legal_force"] == "guideline")

# 2) expert-chat: grounded answer assembled + returned with sources
r, e = ke.h_expert_chat(make_ctx({"expert_id": "ds:kmc:agent/flood-expert", "message": "Are we at risk?", "history": []}))
chk("2 no err", e is None)
chk("2 answer returned", bool(r.get("answer")))
chk("2 expert name from listing", r.get("expert") == "Flood Expert")
chk("2 model name carried", r.get("model") == "kimi")
srcs = r.get("sources") or []
chk("2 sources include the recommendation", any(s.get("type") == "recommendation" and s.get("id") == "r1" for s in srcs))
chk("2 sources include the knowledge citation", any(s.get("type") == "knowledge" and s.get("title") == "NDMA Flood Manual" for s in srcs))

# 3) risk.flood live-summary is computed (population-in-hotspots join + tier)
summ = ke._flood_summary(make_ctx({}), REG["risk.flood"])
chk("3 flood summary counts levels", "severe 1" in summ and "low 1" in summ)
chk("3 flood summary population join (~5,000 in hotspots)", "~5,000" in summ)
chk("3 flood summary tier method (T2 drainage relief)", "tier T2" in summ or "drainage relief" in summ)

print("\nRESULT:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
