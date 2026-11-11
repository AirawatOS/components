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
    if meta.get("egress") or meta.get("secrets"):
        print("  note: egress/secrets/sources DERIVED from aos.egress() — regenerate meta.yaml with `aos meta --write`.")


def cmd_meta(args):
    """Regenerate meta.yaml from code (the derived fields) while PRESERVING curated non-derivable ones
    (display_name/entrypoint/confinement_min/runtime/config/assets). This is what retires hand-maintained
    reads/writes/scopes/files/egress/secrets/sources — the code is the single source, the YAML is generated."""
    m = _load(args.dir)
    if driver.registered() and not hasattr(m, "HANDLERS"):
        args.kind = "driver"
    mp = os.path.join(args.dir, "meta.yaml")
    try:
        import yaml
    except ImportError:
        sys.exit("aos meta --write needs pyyaml (present in the build image) to safely round-trip curated fields; "
                 "here, copy the derived block from `aos check`.")
    existing = (yaml.safe_load(open(mp).read()) if os.path.isfile(mp) else {}) or {}
    # start from the existing file's identity + curated fields; the SDK's base_meta also picks reads/writes
    base = dict(existing); base.update({k: v for k, v in _base_meta(args.dir, args).items() if v is not None})
    derived = M.derive(base)                                  # derived keys (scopes/files/egress/secrets/sources/…) win
    merged = dict(existing); merged.update(derived)           # curated-only keys (runtime/config/assets) survive
    problems = M.validate(merged)
    if problems:
        print("✗ refusing to write — derived meta invalid:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    if getattr(args, "write", False):
        with open(mp, "w") as f:
            yaml.safe_dump(merged, f, sort_keys=False, default_flow_style=False)
        print(f"✓ wrote {mp} (derived fields regenerated; curated fields preserved)")
    else:
        print(yaml.safe_dump(merged, sort_keys=False, default_flow_style=False))
        print("  (dry run — pass --write to update meta.yaml)")


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
    from . import config
    if not getattr(args, "live", False):
        return InMemoryBackend()
    # resolve the target from `aos config` (env still overrides per-run); identity URL optional now that
    # `aos login` caches a token — a cached login is used before any passwordless /login.
    net = config.get("network"); kern = config.get("kernel"); idn = config.get("identity"); apph = config.get("app_host")
    print(f"  (live backend → network {net} · kernel {kern}{' · identity ' + idn if idn else ' · aos-login token'}"
          + (f" · app-host {apph}" if apph else " · app-host UNSET (apps won't deliver — set `aos config set app-host=…`)") + ")")
    return BrokerBackend(args.tenant, net, kern, idn,
                         identity_token=os.environ.get("AIRAWAT_IDENTITY_TOKEN"),
                         dev_user=config.get("dev_user"), token=os.environ.get("RUN_TOKEN"),
                         login_tenant=config.get("login_tenant"), app_host_url=apph,
                         password=config.get("password"))


def cmd_login(args):
    """Browser OIDC login for the CLI — no token pasting. Caches the bearer for `aos deploy --live`."""
    from . import oidc
    fn = oidc.device_login if getattr(args, "device", False) else oidc.login
    tok = fn(issuer=args.issuer, client_id=args.client_id)
    who = ""
    try:                                                       # decode the JWT payload (no verify) for a friendly line
        import base64 as _b, json as _j
        p = tok.split(".")[1]; p += "=" * (-len(p) % 4)
        c = _j.loads(_b.urlsafe_b64decode(p)); who = f" as {c.get('preferred_username') or c.get('sub','?')}"
    except Exception:
        pass
    print(f"✓ logged in{who} · issuer {oidc._issuer(args.issuer)} · token cached ({oidc.CRED})")


def cmd_logout(args):
    from . import oidc
    print(f"✓ logged out of {oidc.logout(args.issuer)}")


def cmd_config(args):
    """Persist the CLI target once (network/kernel/identity/issuer/…) instead of exporting env each run."""
    from . import config
    if args.action == "set":
        pairs = {}
        for kv in args.pairs:
            if "=" not in kv:
                sys.exit(f"expected key=value, got '{kv}'")
            k, v = kv.split("=", 1)
            pairs[k.strip()] = v.strip()
        try:
            config.set_many(pairs)
        except KeyError as e:
            sys.exit(str(e))
        print(f"✓ saved to {config.CONFIG}: {', '.join(pairs)}")
    for k, (v, src) in config.resolved().items():
        print(f"  {k:16} = {v if v is not None else '(unset)':40} [{src}]")


def cmd_deploy(args):
    if not args.sandbox:
        sys.exit("aos deploy currently supports --sandbox only (promotion to prod is a separate flow).")
    from .solution import Deployer
    sol = _load_solution(args)
    backend = _backend_for(args)
    dep = Deployer(backend)
    rep = dep.deploy_sandbox(sol, args.tenant)
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
    if getattr(args, "studio", False):
        # Register the Studio project so the CLI-built solution appears in the in-browser workspace (editable).
        if not args.live:
            print("   ⚠ --studio needs --live (the studio.project record is written to the real registry) — skipped.")
        else:
            pr = dep.push_studio_project(sol, args.tenant)
            print(f"   🎛  Studio project registered: {pr['id']} ({pr['members']} members)")
            print(f"      open Studio → Project menu → “{sol.name}” to edit"
                  + (f"; editable: {', '.join(r.split('/')[-1] for r in pr['build'])}" if pr['build'] else ""))
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


def cmd_upload(args):
    """Push a LARGE local file straight into the platform's content-addressed filestore, over the public
    app-host, without the browser's 25MB cap. Writes the platform.file record (so it shows in Files) and prints
    the file_id to bind to a driver's *_FILE_ID config. Streams the file — never loads it whole into memory."""
    import urllib.request, urllib.parse, urllib.error, mimetypes
    from . import config, oidc
    base = (config.get("app_host") or "").rstrip("/")
    if not base:
        sys.exit("set the app host first:  aos config set app-host=http://13.205.13.220\n"
                 "  (hit the box directly — the TLS front proxy caps bodies at 1MB)")
    path = args.file
    if not os.path.isfile(path):
        sys.exit(f"no such file: {path}")
    size = os.path.getsize(path)
    name = args.name or os.path.basename(path)
    folder = args.folder or "/"
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    # AUTH precedence: explicit AIRAWAT_IDENTITY_TOKEN → a cached `aos login` → a cookie login via the
    # passwordless/password dev /login (the path that works on a deployment without a configured OIDC issuer).
    bearer = os.environ.get("AIRAWAT_IDENTITY_TOKEN") or oidc.cached_token(config.get("oidc_issuer"), config.get("oidc_client_id"))
    cookie = None
    if not bearer:
        tenant = args.tenant or config.get("tenant") or config.get("login_tenant")
        user = args.user or config.get("dev_user") or "devrick"
        if not tenant:
            sys.exit("need a tenant to log in — pass --tenant <t> (or: aos config set tenant=<t>)")
        body = {"realm": tenant, "username": user}
        pw = config.get("password")
        if pw:
            body["password"] = pw
        try:
            lr = urllib.request.urlopen(urllib.request.Request(base + "/login",
                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"), timeout=30)
            for h in (lr.headers.get_all("Set-Cookie") or []):
                if h.startswith("airawat_sid="):
                    cookie = h.split(";", 1)[0]
        except urllib.error.HTTPError as e:
            sys.exit(f"✗ login failed ({e.code}) as {user}@{tenant}: {e.read()[:160].decode(errors='replace')}\n"
                     f"  try a passwordless principal (--user steward) or set a password (aos config set password=…).")
        if not cookie:
            sys.exit("✗ login returned no session cookie")
        print(f"  logged in as {user}@{tenant}")
    headers = {"Content-Type": "application/octet-stream", "Content-Length": str(size), "X-Upload-Type": ctype}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    if cookie:
        headers["Cookie"] = cookie
    url = base + "/bridge/file-push?" + urllib.parse.urlencode({"name": name, "folder": folder, "type": ctype})
    print(f"→ pushing {name} ({size/1048576:.1f} MB) → {base} …")
    with open(path, "rb") as fh:
        req = urllib.request.Request(url, data=fh, method="POST", headers=headers)
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=1800).read() or b"{}")
        except urllib.error.HTTPError as e:
            sys.exit(f"✗ upload failed ({e.code}): {e.read()[:300].decode(errors='replace')}")
        except urllib.error.URLError as e:
            sys.exit(f"✗ could not reach {base}: {e}")
    print(f"✓ uploaded · file_id = {r.get('file_id')}")
    if r.get("file_record"):
        print(f"  platform.file: {r['file_record']}  (folder {folder} · owner {r.get('owner')})")
    elif r.get("record_error"):
        print(f"  ⚠ bytes landed but the Files record failed: {r['record_error']} (you can still use the file_id)")
    print(f"  next → run a driver with it, e.g. in the Console:")
    print(f"    run <driver> <FILE_KEY>={r.get('file_id')}      (e.g. water-complaints COMPLAINTS_FILE_ID=…)")


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
    p = sub.add_parser("meta"); p.add_argument("dir", nargs="?", default="."); p.add_argument("--write", action="store_true", help="regenerate meta.yaml from code (derived fields), preserving curated ones"); p.set_defaults(f=cmd_meta)
    p = sub.add_parser("deploy"); p.add_argument("dir", nargs="?", default="."); p.add_argument("--sandbox", action="store_true"); p.add_argument("--live", action="store_true", help="drive real services over the dev-token (BrokerBackend); default is offline"); p.add_argument("--studio", action="store_true", help="also register a studio.project so the solution appears in Studio (editable); needs --live"); p.add_argument("--tenant", default="you-dev"); p.set_defaults(f=cmd_deploy)
    p = sub.add_parser("destroy"); p.add_argument("dir", nargs="?", default="."); p.add_argument("--sandbox", action="store_true"); p.add_argument("--live", action="store_true", help="tear down the LIVE dev-tenant closure over the dev-token; default is offline"); p.add_argument("--tenant", default="you-dev"); p.set_defaults(f=cmd_destroy)
    p = sub.add_parser("upload", help="push a large local file into the filestore (no 25MB browser cap) + make it a platform.file"); p.add_argument("file"); p.add_argument("--name", help="name to show in Files (default: the file's basename)"); p.add_argument("--folder", default="/", help="Files folder to place it in (default /)"); p.add_argument("--tenant", help="tenant to log in to (default: config tenant)"); p.add_argument("--user", help="login user for the cookie login (default: config dev_user / devrick)"); p.set_defaults(f=cmd_upload)
    p = sub.add_parser("publish"); p.add_argument("dir", nargs="?", default="."); p.add_argument("--target", required=True, choices=["marketplace", "service-directory"]); p.add_argument("--env", default="development"); p.set_defaults(f=cmd_publish)
    p = sub.add_parser("login"); p.add_argument("--issuer"); p.add_argument("--client-id", dest="client_id"); p.add_argument("--device", action="store_true", help="device-code flow (headless; no loopback browser)"); p.set_defaults(f=cmd_login)
    p = sub.add_parser("logout"); p.add_argument("--issuer"); p.set_defaults(f=cmd_logout)
    p = sub.add_parser("config"); p.add_argument("action", nargs="?", default="list", choices=["set", "list"]); p.add_argument("pairs", nargs="*", help="key=value (network=…, issuer=…, kernel=…, identity=…, client-id=…, tenant=…)"); p.set_defaults(f=cmd_config)
    args = ap.parse_args(argv)
    if not hasattr(args, "f"):          # bare `aos` → show help instead of crashing
        ap.print_help()
        return
    args.f(args)


if __name__ == "__main__":
    main()
