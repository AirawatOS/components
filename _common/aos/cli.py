"""aos CLI (P2) — new · check · test · deploy · publish.

Replaces the three overlapping publish scripts + the "know which container to run in"
lore with one command surface. `check` and `new` and local `test` work fully offline;
`deploy`/`publish` carry the dev-vs-prod governance policy (aos.policy) — a developer
builds & tests in their own tenant, but cannot publish into the Marketplace / Service
Directory, and dev artifacts are stamped "Under Development".

    python -m aos check   <dir>
    python -m aos new     engine ds:<tenant>:engine/<name> --verb <verb>
    python -m aos test    <dir>
    python -m aos deploy  <dir> --sandbox
    python -m aos publish <dir> --target marketplace --env development
"""
import argparse, importlib.util, os, sys, json
from . import meta as M, policy, READS, WRITES, driver

ENGINE_TEMPLATE = '''\
"""{title} — {kind} on the aos SDK."""
from aos import engine, ctx


@engine.verb("{verb}", implements="{iface}")
def {fn}(body):
    # read governed facts (auto-declares reads); compute; write governed facts (auto-declares writes)
    rows = ctx.facts("TODO.input").list()
    # ... your logic ...
    return {{"ok": True, "seen": len(rows)}}


HANDLERS = engine.handlers()
'''

DRIVER_TEMPLATE = '''\
"""{title} — a driver on the aos SDK (headless ingest/transform).

The SDK owns the broker transport, tenant resolution, retry, and reads/writes tracking, so a
driver is just: fetch → shape → write. `aos check` derives the meta; `aos test` dry-runs this
against an in-memory registry; `aos deploy --sandbox` runs it in your dev tenant.
"""
from aos import driver, ctx


@driver.main
def run():
    # 1) EGRESS: fetch from your source (plain urllib/httpx — egress is yours, not the broker's).
    # 2) SHAPE: build governed facts (envelope: schema + id, tenant is stamped for you).
    rows = [
        {{"schema": "TODO.output", "id": f"ds:{{ctx.tenant}}:TODO.output/example",
          "observed_at": ctx.now(), "prov": {{}}, "value": 1}},
    ]
    # 3) WRITE: one batched round-trip; auto-declares the write scope for meta.
    n = ctx.write_many(rows)
    print(f"{{n}} fact(s) written to tenant {{ctx.tenant}}")


if __name__ == "__main__":
    driver.run()
'''


def _load(dirpath):
    run = os.path.join(dirpath, "run.py")
    if not os.path.isfile(run):
        sys.exit(f"no run.py in {dirpath}")
    spec = importlib.util.spec_from_file_location("aos_target", run)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _base_meta(dirpath, args):
    """component_id/kind/version/provider from an existing meta.yaml, else from flags/dir."""
    base = {"kind": getattr(args, "kind", None) or "engine", "version": "0.1.0",
            "provider": "ds:airawatdeveloper:developer"}
    mp = os.path.join(dirpath, "meta.yaml")
    if os.path.isfile(mp):
        for line in open(mp):
            line = line.split("#", 1)[0].strip()
            if ":" in line and not line.startswith("-"):
                k, v = line.split(":", 1)
                k, v = k.strip(), v.strip()
                if k in ("component_id", "kind", "version", "provider", "display_name",
                         "entrypoint", "confinement_min"):
                    base[k] = v
                elif k in ("reads", "writes", "files") and v.startswith("[") and v.endswith("]"):
                    base[k] = [x.strip() for x in v[1:-1].split(",") if x.strip()]
    base.setdefault("component_id", getattr(args, "id", None) or f"ds:airawatdeveloper:{base['kind']}/{os.path.basename(os.path.abspath(dirpath))}")
    if base["kind"] == "driver":
        base.setdefault("entrypoint", "run.py")   # a headless driver declares its entrypoint
    return base


def cmd_new(args):
    kind_dir = {"engine": "engine", "driver": "driver", "app": "app"}.get(args.kind, args.kind)
    name = args.id.rsplit("/", 1)[-1]
    d = args.into or name
    os.makedirs(d, exist_ok=True)
    title = name.replace("-", " ").title()
    if args.kind == "driver":
        open(os.path.join(d, "run.py"), "w").write(DRIVER_TEMPLATE.format(title=title))
        print(f"✓ scaffolded driver '{args.id}' → {d}/run.py  (SDK model — @driver.main, ctx, derived meta)")
        print("  next: implement fetch→shape→write, then `aos check` and `aos test`.")
        return
    verb = args.verb or f"{name}-run"
    fn = verb.replace("-", "_")
    iface = args.implements or f"{name.replace('-', '_')}.{fn}@0.1.0"
    open(os.path.join(d, "run.py"), "w").write(ENGINE_TEMPLATE.format(
        title=title, kind=kind_dir, verb=verb, fn=fn, iface=iface))
    print(f"✓ scaffolded {kind_dir} '{args.id}' → {d}/run.py  (SDK model — ctx, derived meta)")
    print("  next: implement the verb, then `aos check` and `aos test`.")


def cmd_check(args):
    m = _load(args.dir)
    if driver.registered() and not hasattr(m, "HANDLERS"):
        args.kind = "driver"          # kind derived from the code (a @driver.main, no HANDLERS)
    meta = M.derive(_base_meta(args.dir, args))
    problems = M.validate(meta)
    print(M.to_yaml(meta))
    if problems:
        print("✗ aos check FAILED:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print("✓ aos check passed — meta valid, vocabulary well-formed.")
    if not READS and not WRITES:
        print("  note: reads/writes are decorator/declared only; run `aos test` to observe them at runtime.")


def cmd_test(args):
    """Local dry-run against an in-memory registry seeded from <dir>/fixtures.json (if present).
    Full dev-tenant sandbox is `aos deploy --sandbox` (P3)."""
    m = _load(args.dir)
    reg = {}
    fx = os.path.join(args.dir, "fixtures.json")
    if os.path.isfile(fx):
        for o in json.load(open(fx)):
            reg[o["id"]] = o
    seeded = len(reg)
    # DRIVER path: run the headless main against an in-memory registry (same ctx surface as live).
    if driver.registered() and not hasattr(m, "HANDLERS"):
        store = driver.run_local(reg, tenant=args.tenant)
        print(f"✓ ran driver against {seeded} seeded record(s); registry now has {len(store)} object(s)")
        print(f"  reads={sorted(READS)} writes={sorted(WRITES)}")
        for oid, obj in list(store.items())[-5:]:
            print(f"   • {obj.get('schema','?')}: {oid}")
        return
    # ENGINE path: dry-run the exported HANDLERS.
    hc = {"kfind": lambda s: [o for o in reg.values() if o.get("schema") == s],
          "kwrite": lambda i, o: reg.__setitem__(i, o), "kget": lambda i: reg.get(i),
          "tenant": args.tenant, "sub": f"user://{args.tenant}/dev", "claims": {}, "body": {}}
    handlers = getattr(m, "HANDLERS", {})
    if not handlers:
        sys.exit("no HANDLERS exported and no @driver.main registered")
    for verb, h in handlers.items():
        res, err = h(hc)
        print(f"  {verb}: {'ERR '+err if err else json.dumps(res)[:160]}")
    print(f"✓ ran {len(handlers)} verb(s) against {seeded} seeded record(s); reads={sorted(READS)} writes={sorted(WRITES)}")


def _load_solution(args):
    from .solution import Solution
    sol = Solution.load(args.dir)
    probs = sol.validate()
    if probs:
        print("✗ solution.yaml invalid:")
        for p in probs:
            print("  -", p)
        sys.exit(1)
    return sol


def _backend_for(args):
    """--live drives real services over the dev-token (BrokerBackend); default is the offline
    InMemoryBackend (proves the pipeline). Live needs ADR-0106 S2 governance on the network."""
    from .solution import InMemoryBackend, BrokerBackend
    if not getattr(args, "live", False):
        return InMemoryBackend()
    net = os.environ.get("AIRAWAT_NETWORK", "http://network:8090")
    kern = os.environ.get("AIRAWAT_KERNEL", "http://kernel:8080")
    idn = os.environ.get("AIRAWAT_IDENTITY", "http://identity:8093")
    print(f"  (live backend → network {net} · kernel {kern})")
    return BrokerBackend(args.tenant, net, kern, idn,
                         identity_token=os.environ.get("AIRAWAT_IDENTITY_TOKEN"),
                         dev_user=os.environ.get("AIRAWAT_DEV_USER", "devrick"),
                         token=os.environ.get("RUN_TOKEN"))


def cmd_deploy(args):
    if not args.sandbox:
        sys.exit("aos deploy currently supports --sandbox only (promotion to prod is a separate flow).")
    from .solution import Deployer
    sol = _load_solution(args)
    backend = _backend_for(args)
    rep = Deployer(backend).deploy_sandbox(sol, args.tenant)
    print(f"→ deploy '{sol.name}' → sandbox tenant '{args.tenant}' (environment: {rep['environment']})")
    print(f"  governance auto-approved in dev: {policy.can_govern_in_place('development')}")
    for step, ref in rep["steps"]:
        print(f"   • {step}: {ref}")
    print(f"   ▸ {rep['marking']['badge']['label']} · open {rep['open'][0]}")
    _pinned = {c: v for c, v in (rep.get("pins") or {}).items() if v}
    if _pinned:
        print(f"   ⇈ pinned: {', '.join(f'{c}@{v}' for c, v in sorted(_pinned.items()))}")
    if rep.get("unpinned"):
        print(f"   ⚠ unpinned reuse (floats to LATEST until pinned): {', '.join(sorted(rep['unpinned']))}")
        print("     → pin them as name@x.y.z in solution.yaml for a reproducible bundle (ADR-0098).")
    if args.live:
        print("✓ whole solution stood up LIVE in the dev tenant over the dev-token "
              "(govern auto-approved · certified · installed · seeded — all confined to development).")
    else:
        print("✓ whole solution stood up in the sandbox (offline backend; add --live to drive real services over the dev-token).")


def cmd_destroy(args):
    if not args.sandbox:
        sys.exit("aos destroy currently supports --sandbox only.")
    from .solution import Deployer
    sol = _load_solution(args)
    backend = _backend_for(args)
    rep = Deployer(backend).destroy_sandbox(sol, args.tenant)
    print(f"→ destroy '{sol.name}' ← sandbox tenant '{args.tenant}'  ({rep['destroyed']})")
    for step in rep["steps"]:
        print(f"   • {step[0]}: {step[1]}  [{step[2]}]")
    print("✓ sandbox closure torn down (idempotent — safe to re-run; a fresh `aos deploy` re-governs from scratch).")


def cmd_publish(args):
    ok, why = policy.check_publish(args.target, args.env)
    print(f"target={args.target} env={args.env}")
    print(("✓ " if ok else "⛔ ") + why)
    if not ok:
        sys.exit(2)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aos", description="Airawat build SDK CLI (P2)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new"); p.add_argument("kind"); p.add_argument("id")
    p.add_argument("--verb"); p.add_argument("--implements"); p.add_argument("--into"); p.set_defaults(f=cmd_new)
    p = sub.add_parser("check"); p.add_argument("dir", nargs="?", default="."); p.set_defaults(f=cmd_check)
    p = sub.add_parser("test"); p.add_argument("dir", nargs="?", default="."); p.add_argument("--tenant", default="you-dev"); p.set_defaults(f=cmd_test)
    p = sub.add_parser("deploy"); p.add_argument("dir", nargs="?", default="."); p.add_argument("--sandbox", action="store_true"); p.add_argument("--live", action="store_true", help="drive real services over the dev-token (BrokerBackend); default is offline"); p.add_argument("--tenant", default="you-dev"); p.set_defaults(f=cmd_deploy)
    p = sub.add_parser("destroy"); p.add_argument("dir", nargs="?", default="."); p.add_argument("--sandbox", action="store_true"); p.add_argument("--live", action="store_true", help="tear down the LIVE dev-tenant closure over the dev-token; default is offline"); p.add_argument("--tenant", default="you-dev"); p.set_defaults(f=cmd_destroy)
    p = sub.add_parser("publish"); p.add_argument("dir", nargs="?", default="."); p.add_argument("--target", required=True, choices=["marketplace", "service-directory"]); p.add_argument("--env", default="development"); p.set_defaults(f=cmd_publish)
    args = ap.parse_args(argv)
    args.f(args)


if __name__ == "__main__":
    main()
