import sys, base64, hashlib
sys.path.insert(0, "/Users/manishsv/Documents/Projects/Airawat/components/_common")
import aos
from aos import Ctx, driver, ctx

# SDK gap #1 — ctx.file (content-addressed filestore) across the three runtimes.
ok = True


def check(label, cond):
    global ok
    print(("  ✓ " if cond else "  ✗ ") + label)
    ok = ok and cond


# ── 1. in-memory (aos test path): put→get roundtrip + digest determinism + dedup ──
print("[in-memory]")
cx = Ctx.in_memory()
tok = aos._current.set(cx)
try:
    fid = ctx.file.put(b"hello filestore")
    check("put returns a content-addressed id", fid == "sha2-256:" + hashlib.sha256(b"hello filestore").hexdigest())
    check("get round-trips the exact bytes", ctx.file.get(fid) == b"hello filestore")
    check("str is utf-8 encoded on put", ctx.file.get(ctx.file.put("héllo")) == "héllo".encode())
    fid2 = ctx.file.put(b"hello filestore")
    check("same bytes → same id (dedup / write-once)", fid2 == fid and len(cx._blobs) == 2)  # 2 distinct blobs stored
    check("absent id → b''", ctx.file.get("sha2-256:deadbeef") in (b"", None))
finally:
    aos._current.reset(tok)

# ── 2. broker shape: fput/fget hit /v1/file/put + /v1/file/get with the right envelope ──
print("[broker envelope]")
seen = []


def _req(method, path, payload=None, tries=4):
    seen.append((method, path, payload))
    if path == "/v1/file/put":
        return {"id": "sha2-256:" + hashlib.sha256(base64.b64decode(payload["content_b64"])).hexdigest(), "size": 3}
    if path == "/v1/file/get":
        return {"id": payload["id"], "content_b64": base64.b64encode(b"BYTES").decode()}
    if path == "/v1/context":
        return {"tenant": "acme-dev"}
    return {}


bcx = aos._broker_ctx(_req, None)
tok = aos._current.set(bcx)
try:
    bid = ctx.file.put(b"abc")
    check("broker put → POST /v1/file/put {content_b64}", seen[-1][:2] == ("POST", "/v1/file/put") and "content_b64" in seen[-1][2])
    got = ctx.file.get(bid)
    check("broker get → POST /v1/file/get {id} → bytes", seen[-1][:2] == ("POST", "/v1/file/get") and got == b"BYTES")
finally:
    aos._current.reset(tok)

# ── 3. capability derivation: using ctx.file marks the `filestore` capability into meta ──
print("[meta derivation]")
check("FILES tracker recorded put+get", aos.FILES == {"put", "get"})
from aos import meta as ameta
m = ameta.derive({"component_id": "ds:acme:driver/blobby", "kind": "driver", "version": "0.1.0", "provider": "acme"})
check("meta.capabilities == [filestore]", m.get("capabilities") == ["filestore"])
check("to_yaml emits capabilities", "capabilities: [filestore]" in ameta.to_yaml(m))

# ── 4. a driver using ctx.file runs end-to-end under run_local (the live code path) ──
print("[driver run_local]")
d = aos._Driver()


@d.main
def go():
    raw = b"raw satellite tile"
    fid = ctx.file.put(raw)
    assert ctx.file.get(fid) == raw
    ctx.write({"schema": "geo.tile", "id": "ds:acme:geo/tile/1", "blob": fid})


store = d.run_local(tenant="acme-dev")
check("driver stored a record referencing the blob id", store["ds:acme:geo/tile/1"]["blob"].startswith("sha2-256:"))

print("\n" + ("✅ ctx.file PROVEN — in-memory, broker envelope, meta capability, live driver path all green."
              if ok else "❌ inspect above"))
sys.exit(0 if ok else 1)
