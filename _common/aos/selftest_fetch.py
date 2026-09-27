import sys
sys.path.insert(0, "/Users/manishsv/Documents/Projects/Airawat/components/_common")
import aos
from aos import Ctx, ctx, Reject

# SDK gap #3 — brokered egress with server-side secret injection: ctx.fetch through /v1/fetch.
ok = True
def check(label, cond):
    global ok; print(("  ✓ " if cond else "  ✗ ") + label); ok = ok and cond

seen = []
def _req(method, path, payload=None, tries=4):
    seen.append((method, path, payload))
    if path == "/v1/fetch":
        return {"upstream_status": 200, "body": '{"hotspots": 3}', "headers": {"content-type": "application/json"}}
    return {}

# ── broker: correct /v1/fetch envelope + secret injection + FetchResult ──
print("[broker fetch]")
aos.EGRESS.clear()
cx = aos._broker_ctx(_req, {})
tok = aos._current.set(cx)
try:
    r = ctx.fetch("https://firms.modaps.eosdis.nasa.gov/api/x?MAP_KEY={KEY}", inject="FIRMS_MAP_KEY")
    body = seen[-1][2]
    check("POST /v1/fetch", seen[-1][:2] == ("POST", "/v1/fetch"))
    check("inject shorthand str → {name}", body.get("inject") == {"name": "FIRMS_MAP_KEY"})
    check("default method GET", body.get("method") == "GET")
    check("FetchResult.status / .ok", r.status == 200 and r.ok)
    check("FetchResult.json() parses body", r.json() == {"hotspots": 3})
    check("egress host tracked", "firms.modaps.eosdis.nasa.gov" in aos.EGRESS)
    # POST with a body (Overpass QL) + explicit inject dict
    ctx.fetch("https://overpass.example/api/interpreter", method="POST", body="[out:json];node;out;",
              inject={"name": "OVERPASS_TOKEN"})
    body = seen[-1][2]
    check("POST forwards method + body", body["method"] == "POST" and body["body"].startswith("[out:json]"))
    check("explicit inject dict passes through", body["inject"] == {"name": "OVERPASS_TOKEN"})
    # keyless call: no inject key on the wire
    ctx.fetch("https://open.example/data.json")
    check("no inject → no inject key", "inject" not in seen[-1][2])
finally:
    aos._current.reset(tok)

# ── in-memory: fixture-driven egress for aos test (no network, no fabrication) ──
print("[in-memory fixture]")
cx = Ctx.in_memory(tenant="t", fetches={"https://api.example/v": {"upstream_status": 200, "body": "OK"}})
tok = aos._current.set(cx)
try:
    check("fixture response returned", ctx.fetch("https://api.example/v").body == "OK")
    missing = False
    try:
        ctx.fetch("https://api.example/other")
    except Reject:
        missing = True
    check("no fixture → Reject (honest, not fabricated)", missing)
finally:
    aos._current.reset(tok)

# ── host: no brokered-egress cap → clean Reject (in-process engines don't do confined egress) ──
print("[host adapter]")
hc = {"kfind": lambda s: [], "kwrite": lambda i, o: None, "tenant": "t", "body": {}}
hcx = Ctx.from_host(hc)
tok = aos._current.set(hcx)
try:
    raised = False
    try:
        ctx.fetch("https://x/y")
    except Reject:
        raised = True
    check("host without fetch cap → Reject", raised)
finally:
    aos._current.reset(tok)

print("\n" + ("✅ ctx.fetch PROVEN — brokered egress envelope + secret injection, FetchResult, in-memory fixture, host guard."
              if ok else "❌ inspect above"))
sys.exit(0 if ok else 1)
