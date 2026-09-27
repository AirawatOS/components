import sys
sys.path.insert(0, "/Users/manishsv/Documents/Projects/Airawat/components/_common")
import aos
from aos import Ctx, ctx, Reject

# SDK gap #5 — host-only ctx capabilities surfaced on ctx (kfind_x cross-tenant, http, network_url, embed, …).
ok = True
def check(label, cond):
    global ok; print(("  ✓ " if cond else "  ✗ ") + label); ok = ok and cond

# ── host adapter: capabilities forwarded from the injected engine ctx dict ──
print("[host adapter]")
calls = []
hc = {
    "kfind": lambda s: [], "kwrite": lambda i, o: None, "tenant": "airawat", "body": {},
    "kfind_x": lambda schema, target, where=None: [{"id": f"ds:{target}:{schema}/1", "schema": schema, "tenant": target}],
    "http": lambda m, u, p=None, t=None, h=None: calls.append(("http", m, u)) or {"ok": True},
    "network_url": "http://network:8090", "kernel_url": "http://kernel:8080",
    "embed": lambda texts, kind="query": {"dim": 3, "vectors": [[0.1, 0.2, 0.3]]},
    "ensure_install": lambda cid: {"installed": cid},
    "federate": lambda pv, wf, ti, pl, pc: {"remote_case": "rc-1"},
    "remote_status": lambda pv, rc: {"status": "open"},
}
cx = Ctx.from_host(hc)
tok = aos._current.set(cx)
try:
    rows = ctx.facts_x("finance.fund_flow", "ministry-x").list()
    check("facts_x → cross-tenant rows for the target tenant", rows and rows[0]["tenant"] == "ministry-x")
    check("facts_x records the READ scope", "finance.fund_flow" in aos.READS)
    ctx.http("POST", "http://network:8090/v1/x", {"a": 1})
    check("ctx.http forwards to host http", calls and calls[-1] == ("http", "POST", "http://network:8090/v1/x"))
    check("ctx.network_url / kernel_url exposed", ctx.network_url == "http://network:8090" and ctx.kernel_url == "http://kernel:8080")
    check("ctx.embed via host gateway", ctx.embed(["hi"])["dim"] == 3)
    check("ctx.ensure_install", ctx.ensure_install("ds:t:driver/x")["installed"] == "ds:t:driver/x")
    check("ctx.federate + remote_status", ctx.federate("p", "wf", "t", {}, None)["remote_case"] == "rc-1"
                                       and ctx.remote_status("p", "rc-1")["status"] == "open")
finally:
    aos._current.reset(tok)

# ── in-memory: kfind_x reads cross-tenant from the shared store (aos test for aggregator engines) ──
print("[in-memory kfind_x]")
store = {"ds:a:finance.fund_flow/1": {"id": "ds:a:finance.fund_flow/1", "schema": "finance.fund_flow", "tenant": "a", "amt": 10},
         "ds:b:finance.fund_flow/1": {"id": "ds:b:finance.fund_flow/1", "schema": "finance.fund_flow", "tenant": "b", "amt": 20}}
cx = Ctx.in_memory(store, tenant="airawat")
tok = aos._current.set(cx)
try:
    a = ctx.facts_x("finance.fund_flow", "a").list()
    check("in-memory facts_x isolates the target tenant", len(a) == 1 and a[0]["amt"] == 10)
finally:
    aos._current.reset(tok)

# ── confined (broker/headless): host caps are NOT granted → clean Reject, not a crash ──
print("[broker guard]")
seen = []
cx = aos._broker_ctx(lambda *a, **k: seen.append(a) or {"tenant": "t"}, {})
tok = aos._current.set(cx)
try:
    for name, fn in [("facts_x", lambda: ctx.facts_x("s", "t")), ("http", lambda: ctx.http("GET", "u")),
                     ("embed", lambda: ctx.embed(["x"])), ("ensure_install", lambda: ctx.ensure_install("c"))]:
        r = False
        try: fn()
        except Reject: r = True
        check(f"confined ctx.{name} → Reject", r)
    check("confined ctx.network_url is None", ctx.network_url is None)
finally:
    aos._current.reset(tok)

print("\n" + ("✅ host-caps PROVEN — facts_x/http/network_url/embed/ensure_install/federate on host; testable in-memory; confined guarded."
              if ok else "❌ inspect above"))
sys.exit(0 if ok else 1)
