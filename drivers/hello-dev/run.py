#!/usr/bin/env python3
"""hello-dev — the smallest driver that exercises the INT-0004 dev-test path end-to-end.

It takes its tenant from the broker CONTEXT (the run-token decides it — a dev-token pins it to the
developer tenant), writes one `demo.ping` fact, reads it back, and fails loudly if anything is off.
The ONLY I/O is the kernel broker via `call()`, keyed by the run-token in the clean environment.
"""
import json, os, time, urllib.request


def call(path, body):
    req = urllib.request.Request(
        os.environ["AIRAWAT_API"] + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Run-Token": os.environ["RUN_TOKEN"]},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_context():
    """/v1/context is a GET keyed by the run-token — the run's identity + tenant + effective capabilities."""
    req = urllib.request.Request(os.environ["AIRAWAT_API"] + "/v1/context",
                                 headers={"X-Run-Token": os.environ["RUN_TOKEN"]})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    ctx = get_context()                                    # tenant comes from the run identity, never hardcoded
    tenant = ctx.get("tenant")
    if not tenant:
        raise SystemExit("FATAL: broker context carries no tenant — cannot address a write")
    print(f"context: run={ctx.get('run_id')} tenant={tenant}")

    fid = f"ds:{tenant}:demo.ping/{int(time.time())}"
    obj = {"schema": "demo.ping", "id": fid, "tenant": tenant, "note": "hello from the dev sandbox"}
    call("/v1/write", {"scope": "demo.ping", "id": fid, "object": obj})
    print(f"wrote {fid}")

    got = call("/v1/read", {"scope": "demo.ping", "id": fid}).get("object")
    if not got or got.get("id") != fid:
        raise SystemExit(f"FATAL: read-back mismatch for {fid}: {got!r}")
    print(f"read back OK: {got['id']}  note={got.get('note')!r}")
    print("hello-dev: end-to-end write+read in the dev tenant SUCCEEDED")


if __name__ == "__main__":
    main()
