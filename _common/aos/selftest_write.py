import sys
sys.path.insert(0, "/Users/manishsv/Documents/Projects/Airawat/components/_common")
import aos
from aos import Ctx, ctx

# SDK gap #2 — bitemporal write envelope: ctx.write / write_many can express valid_time + asserted_at.
ok = True
def check(label, cond):
    global ok; print(("  ✓ " if cond else "  ✗ ") + label); ok = ok and cond

seen = []
def _req(method, path, payload=None, tries=4):
    seen.append((method, path, payload)); return {"stored": "ref", "receipt": "r"} if "write" in path else {}

# ── broker envelope: valid_from/valid_to → {from,to}; asserted_at forwarded ──
print("[broker write]")
cx = aos._broker_ctx(_req, {})           # body={} → skips /v1/context
tok = aos._current.set(cx)
try:
    ctx.write({"schema": "air.pm25", "id": "ds:t:air/pm25/1", "value": 88}, valid_from="2026-01-01T00:00:00Z")
    body = seen[-1][2]
    check("POST /v1/write carries valid_time.from", seen[-1][1] == "/v1/write" and body.get("valid_time") == {"from": "2026-01-01T00:00:00Z"})
    ctx.write({"schema": "air.pm25", "id": "ds:t:air/pm25/2", "value": 90},
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-01-02T00:00:00Z", asserted_at="2026-01-03T00:00:00Z")
    body = seen[-1][2]
    check("valid_from+valid_to → {from,to}", body["valid_time"] == {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z"})
    check("asserted_at forwarded", body["asserted_at"] == "2026-01-03T00:00:00Z")
    ctx.write({"schema": "geo.building", "id": "ds:t:geo/building/1"})
    check("no valid_time → plain ENTITY write (no envelope keys)", "valid_time" not in seen[-1][2] and "asserted_at" not in seen[-1][2])
    # explicit valid_time dict passes through verbatim
    ctx.write({"schema": "air.pm25", "id": "ds:t:air/pm25/3"}, valid_time={"from": "x", "to": "y"})
    check("explicit valid_time dict passes through", seen[-1][2]["valid_time"] == {"from": "x", "to": "y"})
finally:
    aos._current.reset(tok)

# ── batch: per-row valid_from_field lifts each observed_at into valid_time; one asserted_at for the batch ──
print("[broker write_many]")
seen.clear()
cx = aos._broker_ctx(_req, {})
tok = aos._current.set(cx)
try:
    n = ctx.write_many([{"schema": "air.pm25", "id": "ds:t:air/pm25/a", "observed_at": "2026-01-01T00:00:00Z"},
                        {"schema": "air.pm25", "id": "ds:t:air/pm25/b", "observed_at": "2026-01-01T01:00:00Z"}],
                       valid_from_field="observed_at", asserted_at="2026-02-01T00:00:00Z")
    body = seen[-1][2]
    check("batch wrote 2", n == 2 and seen[-1][1] == "/v1/write/batch" and len(body["items"]) == 2)
    check("per-row valid_time from observed_at", body["items"][0]["valid_time"] == {"from": "2026-01-01T00:00:00Z"}
                                              and body["items"][1]["valid_time"] == {"from": "2026-01-01T01:00:00Z"})
    check("one asserted_at for the batch", body["asserted_at"] == "2026-02-01T00:00:00Z")
finally:
    aos._current.reset(tok)

# ── in-memory (aos test) tolerates the envelope: write + write_many still store objects ──
print("[in-memory tolerance]")
cx = Ctx.in_memory(tenant="t")
tok = aos._current.set(cx)
try:
    ctx.write({"schema": "air.pm25", "id": "ds:t:air/pm25/x"}, valid_from="2026-01-01T00:00:00Z", asserted_at="z")
    ctx.write_many([{"schema": "air.pm25", "id": "ds:t:air/pm25/y", "observed_at": "2026-01-01T00:00:00Z"}],
                   valid_from_field="observed_at")
    check("in-memory stored both despite envelope", cx._store.get("ds:t:air/pm25/x") and cx._store.get("ds:t:air/pm25/y"))
finally:
    aos._current.reset(tok)

# ── host adapter: forwards the envelope when the host write accepts it, else degrades ──
print("[host adapter]")
got = {}
def new_host_write(oid, obj, valid_time=None, asserted_at=None):
    got["vt"] = valid_time; return {"ok": True}
kw = aos._host_kwrite(new_host_write)
kw("id1", {"a": 1}, valid_time={"from": "t0"})
check("new host write receives valid_time", got.get("vt") == {"from": "t0"})
legacy_calls = []
def legacy_host_write(oid, obj):     # predates the envelope — only (oid, obj)
    legacy_calls.append((oid, obj)); return {"ok": True}
aos._host_kwrite(legacy_host_write)("id2", {"b": 2}, valid_time={"from": "t1"})
check("legacy host write still succeeds (graceful fallback)", legacy_calls == [("id2", {"b": 2})])

print("\n" + ("✅ write-envelope PROVEN — valid_time/asserted_at on write + write_many; broker, batch, in-memory, host all correct."
              if ok else "❌ inspect above"))
sys.exit(0 if ok else 1)
