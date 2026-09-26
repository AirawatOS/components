"""hello-dev-aos — the hello-dev reference driver, rewritten on the aos SDK.

Behaviourally identical to drivers/hello-dev (write one demo.ping fact, read it back, fail loudly),
but the broker transport, tenant resolution, retry, and reads/writes tracking are the SDK's — the
driver is just the logic. Compare to hello-dev/run.py: ~50 lines of hand-rolled urllib → ~12 here.
"""
from aos import driver, ctx


@driver.main
def run():
    tenant = ctx.tenant                          # resolved from the broker context by the SDK
    if not tenant:
        raise SystemExit("FATAL: broker context carries no tenant — cannot address a write")

    fid = f"ds:{tenant}:demo.ping/{ctx.now()[:19]}"
    ctx.write(schema="demo.ping", id=fid, note="hello from the dev sandbox")   # tenant stamped; write tracked

    got = ctx.get(fid)                            # read-back (read scope tracked)
    if not got or got.get("id") != fid:
        raise SystemExit(f"FATAL: read-back mismatch for {fid}: {got!r}")
    print(f"hello-dev-aos: end-to-end write+read in tenant {tenant} SUCCEEDED ({fid})")


if __name__ == "__main__":
    driver.run()
