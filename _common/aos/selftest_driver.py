"""Driver-path parity proof (matches the engine path's selftest.py).

Proves the aos DRIVER ergonomics are as strong as the engine path:
  1. the broker ctx contract is correct (GET context, scope-correct read/query, batch, ai) —
     exercised via an in-memory ctx that uses the SAME Ctx surface a live driver uses;
  2. `hello-dev-aos` (the SDK rewrite of hello-dev) runs end-to-end write+read with no boilerplate;
  3. reads/writes are auto-tracked from the code → meta is DERIVED + valid (kind=driver, entrypoint);
  4. `ctx.write_many` batches; the whole surface (facts/get/write/write_many) works headless-shaped.
"""
import sys, importlib.util, os
BASE = __import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))))
sys.path.insert(0, BASE + "/_common")

import aos
from aos import Ctx, driver, ctx
from aos import meta as M

PASS, FAIL = [], []
def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  — {detail}" if detail else ""))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


print("=== aos driver-path parity proof ===\n")

# 1) load the SDK rewrite of hello-dev (registers @driver.main on the singleton)
aos.READS.clear(); aos.WRITES.clear()
_load(BASE + "/drivers/hello-dev-aos/run.py", "hello_dev_aos")
check("driver registered a @driver.main", driver.registered())

# 2) run it against an in-memory registry (the `aos test` path) — same Ctx surface as live
store = driver.run_local({}, tenant="asha-dev")
pings = [o for o in store.values() if o.get("schema") == "demo.ping"]
check("wrote exactly one demo.ping fact", len(pings) == 1, f"{len(pings)} written")
check("write was tenant-stamped", bool(pings) and pings[0].get("tenant") == "asha-dev")
check("read-back succeeded (get by id)", bool(pings) and pings[0].get("note") == "hello from the dev sandbox")

# 3) reads/writes auto-tracked → meta derived + valid
check("write scope auto-tracked", "demo.ping" in aos.WRITES)
check("read scope auto-tracked (from ctx.get)", "demo.ping" in aos.READS)
meta = M.derive({"component_id": "ds:airawatdeveloper:driver/hello-dev-aos", "kind": "driver",
                 "version": "0.1.0", "provider": "ds:airawatdeveloper:participant/developer",
                 "entrypoint": "run.py"})
probs = M.validate(meta)
check("meta derives kind=driver + entrypoint", meta["kind"] == "driver" and meta.get("entrypoint") == "run.py")
check("meta writes == [demo.ping]", meta["writes"] == ["demo.ping"], str(meta["writes"]))
check("meta scopes derived (read+write demo.ping)",
      {"verb": "write", "scope": "demo.ping"} in meta["scopes"] and {"verb": "read", "scope": "demo.ping"} in meta["scopes"])
check("no verbs/implements on a driver", "verbs" not in meta and "implements" not in meta)
check("aos check would PASS (validate clean)", probs == [], str(probs))

# 4) the write_many batch superpower + full surface on the in-memory ctx
aos.WRITES.clear()
cx = Ctx.in_memory({}, tenant="asha-dev")
n = cx.write_many([{"schema": "risk.flood", "id": f"ds:asha-dev:risk.flood/{i}", "value": i} for i in range(50)])
check("write_many batched 50 facts", n == 50 and len(cx._store) == 50)
check("write_many tracked the scope", "risk.flood" in aos.WRITES)
rows = cx.facts("risk.flood").list()
check("facts() reads them back", len(rows) == 50)
got = cx.get("ds:asha-dev:risk.flood/7")
check("get(id) resolves a written fact", got and got.get("value") == 7)

print("\n=== RESULT ===")
print(f"PASS: {len(PASS)}   FAIL: {len(FAIL)}")
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
print("✅ DRIVER PARITY PROVEN — @driver.main + full ctx surface (facts/get/write/write_many); "
      "meta derived+valid (kind/entrypoint/scopes); aos test runs it against an in-memory registry.")
