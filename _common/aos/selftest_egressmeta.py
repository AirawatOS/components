import sys
sys.path.insert(0, "/Users/manishsv/Documents/Projects/Airawat/components/_common")
import aos
from aos import meta as ameta, egress, Ctx, ctx

# SDK gap #4 — egress/secrets/sources DERIVED from code (aos.egress + observed ctx.fetch), retiring the YAML blocks.
ok = True
def check(label, cond):
    global ok; print(("  ✓ " if cond else "  ✗ ") + label); ok = ok and cond

# ── declare two egress hosts once, at module level (as a driver would at import) ──
egress("nominatim.openstreetmap.org", rate_per_min=30,
       user_agent="AirawatOS/geocode-ledger (+https://airawat.org; manishsv@gmail.com)",
       provides=["tax.assessment"], source_ref="ds:airawat:ref/source/nominatim")
egress("firms.modaps.eosdis.nasa.gov", rate_per_min=20, user_agent="AirawatOS/firms-fire",
       secret="FIRMS_MAP_KEY", provides=["signal.fire"], source_ref="ds:airawat:ref/source/firms")

m = ameta.derive({"component_id": "ds:airawat:driver/x", "kind": "driver", "version": "0.2.0", "provider": "ds:airawat:developer"})

print("[egress derivation]")
check("meta.egress = host+rate+UA per declared host (sorted)",
      m["egress"] == [{"host": "firms.modaps.eosdis.nasa.gov", "rate_per_min": 20, "user_agent": "AirawatOS/firms-fire"},
                      {"host": "nominatim.openstreetmap.org", "rate_per_min": 30,
                       "user_agent": "AirawatOS/geocode-ledger (+https://airawat.org; manishsv@gmail.com)"}])
check("secret is NOT in the egress block (it's separate)", all("secret" not in e for e in m["egress"]))

print("[secrets derivation]")
check("meta.secrets derived from the declared secret", m["secrets"] == ["FIRMS_MAP_KEY"])

print("[sources derivation]")
check("meta.sources = {ref,host,provides} per declared source",
      m["sources"] == [{"ref": "ds:airawat:ref/source/firms", "host": "firms.modaps.eosdis.nasa.gov", "provides": ["signal.fire"]},
                       {"ref": "ds:airawat:ref/source/nominatim", "host": "nominatim.openstreetmap.org", "provides": ["tax.assessment"]}])

print("[to_yaml]")
y = ameta.to_yaml(m)
check("to_yaml emits egress + secrets + sources", "egress:" in y and "secrets: [FIRMS_MAP_KEY]" in y and "sources:" in y)

print("[observed ctx.fetch secret → secrets]")
def _req(method, path, payload=None, tries=4):
    return {"upstream_status": 200, "body": "{}"} if path == "/v1/fetch" else {}
cx = aos._broker_ctx(_req, {})
tok = aos._current.set(cx)
try:
    ctx.fetch("https://gateway.example/x", inject="MOSPI_GATEWAY_TOKEN")   # a secret NOT pre-declared
finally:
    aos._current.reset(tok)
m2 = ameta.derive({"component_id": "ds:airawat:driver/x", "kind": "driver", "version": "0.2.0", "provider": "ds:airawat:developer"})
check("an injected-but-undeclared secret is still captured into meta.secrets", "MOSPI_GATEWAY_TOKEN" in m2["secrets"])

print("\n" + ("✅ egress-meta PROVEN — egress/secrets/sources derive from aos.egress + observed ctx.fetch (YAML re-attach retired)."
              if ok else "❌ inspect above"))
sys.exit(0 if ok else 1)
