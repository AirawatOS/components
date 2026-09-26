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
from . import meta as M, policy, READS, WRITES

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
                if k in ("component_id", "kind", "version", "provider", "display_name"):
                    base[k] = v
                elif k in ("reads", "writes") and v.startswith("[") and v.endswith("]"):
                    base[k] = [x.strip() for x in v[1:-1].split(",") if x.strip()]
    base.setdefault("component_id", getattr(args, "id", None) or f"ds:airawatdeveloper:engine/{os.path.basename(os.path.abspath(dirpath))}")
    return base


def cmd_new(args):
    kind_dir = {"engine": "engine", "driver": "driver", "app": "app"}.get(args.kind, args.kind)
    name = args.id.rsplit("/", 1)[-1]
    d = args.into or name
    os.makedirs(d, exist_ok=True)
    verb = args.verb or f"{name}-run"
    fn = verb.replace("-", "_")
    iface = args.implements or f"{name.replace('-', '_')}.{fn}@0.1.0"
    open(os.path.join(d, "run.py"), "w").write(ENGINE_TEMPLATE.format(
        title=name.replace("-", " ").title(), kind=kind_dir, verb=verb, fn=fn, iface=iface))
    print(f"✓ scaffolded {kind_dir} '{args.id}' → {d}/run.py  (SDK model — ctx, derived meta)")
    print("  next: implement the verb, then `aos check` and `aos test`.")


def cmd_check(args):
    _load(args.dir)
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
    hc = {"kfind": lambda s: [o for o in reg.values() if o.get("schema") == s],
          "kwrite": lambda i, o: reg.__setitem__(i, o), "kget": lambda i: reg.get(i),
          "tenant": args.tenant, "sub": f"user://{args.tenant}/dev", "claims": {}, "body": {}}
    handlers = getattr(m, "HANDLERS", {})
    if not handlers:
        sys.exit("no HANDLERS exported")
    for verb, h in handlers.items():
        res, err = h(hc)
        print(f"  {verb}: {'ERR '+err if err else json.dumps(res)[:160]}")
    print(f"✓ ran {len(handlers)} verb(s) against {len(reg)} seeded record(s); reads={sorted(READS)} writes={sorted(WRITES)}")


def cmd_deploy(args):
    if not args.sandbox:
        sys.exit("aos deploy currently supports --sandbox only (P3 will add promotion).")
    print(f"→ deploy to sandbox tenant '{args.tenant}' (environment: development)")
    print("  · governance auto-approved in dev:", policy.can_govern_in_place("development"))
    print("  · artifacts stamped:", policy.dev_marking({"environment": "development"})["badge"]["label"])
    print("  (P3: this will govern schemas/roles, build+certify members, install the whole")
    print("   solution into your dev tenant, and open it for live exercise.)")


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
    p = sub.add_parser("deploy"); p.add_argument("dir", nargs="?", default="."); p.add_argument("--sandbox", action="store_true"); p.add_argument("--tenant", default="you-dev"); p.set_defaults(f=cmd_deploy)
    p = sub.add_parser("publish"); p.add_argument("dir", nargs="?", default="."); p.add_argument("--target", required=True, choices=["marketplace", "service-directory"]); p.add_argument("--env", default="development"); p.set_defaults(f=cmd_publish)
    args = ap.parse_args(argv)
    args.f(args)


if __name__ == "__main__":
    main()
