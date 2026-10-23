"""aos.solution — the declarative solution manifest + `aos deploy --sandbox` pipeline (ADR-0106 S1).

`solution.yaml` describes a whole vertical (schemas, roles, workflow graph, reuse+build
members, viz.layers, decider, subscriptions, contributions). The Deployer runs ONE
idempotent pipeline over it — govern → build+certify → compose+install → seed → open —
confined to the developer's tenant, stamping every artifact "Under Development".

The pipeline talks to the platform through a small `Backend` protocol, so it is testable
offline against `InMemoryBackend` and runs for real against `BrokerBackend` (dev-token).
"""
import os, sys, json, re
from . import meta as M, policy

_ROLE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9-]*$")   # <domain>.<name> — hyphens allowed (safety.ground-marshal)


# ── manifest ────────────────────────────────────────────────────────────────
class Solution:
    def __init__(self, data, source_dir="."):
        self.d = data or {}
        self.dir = source_dir

    @classmethod
    def load(cls, path):
        """Load solution.yaml. Prefers PyYAML (present in the reference runtime); falls back
        to a small inline-only reader (top-level keys + one-level nested maps + inline
        [lists]/{maps}) so the SDK has no hard dependency."""
        if os.path.isdir(path):
            path = os.path.join(path, "solution.yaml")
        return cls(_load_yaml(open(path).read()), os.path.dirname(os.path.abspath(path)))

    # typed accessors with safe defaults
    @property
    def name(self): return self.d.get("solution")
    @property
    def environment(self): return self.d.get("environment", policy.DEV)
    @property
    def schemas(self): return self.d.get("schemas") or []
    @property
    def roles(self): return self.d.get("roles") or []
    @property
    def interfaces(self): return self.d.get("interfaces") or []
    @property
    def workflow(self): return self.d.get("workflow")
    @property
    def reuse(self): return (self.d.get("members") or {}).get("reuse") or []
    @property
    def build(self): return (self.d.get("members") or {}).get("build") or []
    @property
    def viz_layers(self): return self.d.get("viz_layers") or []
    @property
    def decider(self): return self.d.get("decider")
    @property
    def subscriptions(self): return self.d.get("subscriptions") or []
    @property
    def contributions(self): return self.d.get("contributions") or []
    @property
    def setup(self): return self.d.get("setup") or []   # install-seed steps: [{run: <component>[, env:{…}]}]

    def validate(self):
        p = []
        if not self.name:
            p.append("solution: name is required")
        for s in self.schemas:
            sid = s.get("id") if isinstance(s, dict) else s
            if not M._NOUN.match(str(sid or "")):
                p.append(f"schema id '{sid}' must be a governed noun <domain>.<subject>")
        for r in self.roles:
            if not _ROLE.match(str(r)):
                p.append(f"role '{r}' must be <domain>.<name>")
        if not self.reuse and not self.build:
            p.append("members: at least one reuse or build member is required")
        return p


# ── backend protocol + an offline implementation ─────────────────────────────
class InMemoryBackend:
    """Records every platform effect, so the pipeline is unit-testable + idempotency
    is provable. Mirrors what BrokerBackend does over the dev-token."""
    def __init__(self):
        self.governed_schemas = {}     # id -> record (tenant-scoped in dev)
        self.governed_roles = set()
        self.built = set()
        self.installed = {}            # solution_id -> tenant
        self.viz = set()
        self.contributions = []
        self.subs = {}                 # subscription_id -> record
        self.log = []

    def govern_schema(self, sid, defn, tenant, environment):
        gid = _dev_scope(sid, tenant) if policy.is_dev(environment) else sid
        first = gid not in self.governed_schemas
        self.governed_schemas[gid] = {"id": gid, "def": defn, "approved_by": ("dev-auto" if policy.is_dev(environment) else "standards-body")}
        self.log.append(("govern_schema", gid, "new" if first else "noop"))
        return gid

    def govern_role(self, role, tenant, environment):
        first = role not in self.governed_roles
        self.governed_roles.add(role)
        self.log.append(("govern_role", role, "new" if first else "noop"))

    def build_member(self, ref, tenant, environment, base_dir=None):
        first = ref not in self.built          # offline: no real delivery (base_dir ignored)
        self.built.add(ref)
        self.log.append(("build+certify", ref, "new" if first else "noop"))

    def compose_install(self, solution_name, members, tenant, environment):
        sid = f"ds:{tenant}:solution/{solution_name}"
        first = sid not in self.installed
        self.installed[sid] = tenant
        self.log.append(("compose+install", sid, "new" if first else "noop"))
        return sid

    def seed_viz(self, layer, tenant):
        first = layer not in self.viz
        self.viz.add(layer)
        self.log.append(("seed_viz", layer, "new" if first else "noop"))

    def seed_contribution(self, rec, tenant):
        self.contributions.append(rec)
        self.log.append(("seed_contribution", rec, "new"))

    def seed_subscription(self, sub, tenant, solution_name="solution"):
        sid, rec = _subscription_record(sub, tenant, solution_name)
        first = sid not in self.subs
        self.subs[sid] = rec
        self.log.append(("seed_subscription", f"{sub.get('on')}→{sub.get('do')}", "new" if first else "noop"))

    def open_urls(self, solution_name, tenant):
        return [f"https://…/{tenant}/{solution_name}"]

    def write_project(self, rec):
        self.log.append(("studio-project", rec["id"], f"{len(rec.get('members', []))} members (offline — not written)"))

    # ── teardown (ADR-0106 S5) — reverse the closure, idempotent ──
    def unseed_viz(self, layer, tenant):
        found = layer in self.viz; self.viz.discard(layer)
        self.log.append(("unseed_viz", layer, "ok" if found else "gone"))

    def unseed_contribution(self, rec, tenant):
        if rec in self.contributions:
            self.contributions.remove(rec)
        self.log.append(("unseed_contribution", rec, "ok"))

    def uninstall(self, solution_name, members, tenant):
        sid = f"ds:{tenant}:solution/{solution_name}"
        found = sid in self.installed; self.installed.pop(sid, None)
        self.log.append(("uninstall", sid, "ok" if found else "gone"))

    def retire_member(self, ref, tenant):
        found = ref in self.built; self.built.discard(ref)
        self.log.append(("retire", ref, "ok" if found else "gone"))

    def retract_schema(self, sid, tenant):
        gid = _dev_scope(sid, tenant)
        found = gid in self.governed_schemas; self.governed_schemas.pop(gid, None)
        self.log.append(("retract_schema", gid, "ok" if found else "gone"))

    def retract_role(self, role, tenant):
        found = role in self.governed_roles; self.governed_roles.discard(role)
        self.log.append(("retract_role", role, "ok" if found else "gone"))

    def unseed_subscription(self, sub, tenant, solution_name="solution"):
        sid, _ = _subscription_record(sub, tenant, solution_name)
        found = sid in self.subs; self.subs.pop(sid, None)
        self.log.append(("unseed_subscription", f"{sub.get('on')}→{sub.get('do')}", "ok" if found else "gone"))


# ── live backend: drives the platform over the developer's dev-token (INT-0004) ──
class BrokerBackend:
    """Runs the pipeline against LIVE services, confined to the dev tenant by a dev-token
    (INT-0004). Same protocol as InMemoryBackend. Requires ADR-0106 S2 live on the network
    for dev-tenant governance auto-approval. `transport` is injectable for testing — it takes
    (method, url, body, token) and returns a dict; the default is a urllib transport with retry.
    """
    def __init__(self, tenant, network_url, kernel_url, identity_url=None, identity_token=None,
                 dev_user="devrick", token=None, transport=None, login_tenant=None, app_host_url=None,
                 password=None):
        self.tenant = tenant
        # reference-IdP password (only needed when that IdP has been flipped off passwordless, ADR-0068);
        # env/config supplies it for non-interactive runs, else `_login` prompts on a tty. The Keycloak
        # `aos login` path carries its own password through the browser and ignores this.
        self._password = password or os.environ.get("AIRAWAT_PASSWORD")
        self.net = network_url.rstrip("/")
        self.kernel = kernel_url.rstrip("/")
        self.idn = (identity_url or "").rstrip("/")
        # the app-host that serves this tenant's desktop — where an APP bundle must be DELIVERED (overlay)
        # for the tile to appear. Without it, --live still governs + installs-by-reference but can't deliver code.
        self.app = (app_host_url or "").rstrip("/")
        # the developer's HOME tenant for login (EC2 identity needs {username,tenant}); defaults to the
        # deploy tenant, overridable (a developer logging in to promote INTO another tenant).
        self._login_tenant = login_tenant or os.environ.get("AIRAWAT_LOGIN_TENANT") or tenant
        self._t = transport or _urllib_transport
        self.log = []
        # TWO tokens, two planes (surfaced by the local live proof):
        #  • identity token — the developer's JWT; authenticates NETWORK governance/certify/solutions
        #    (verified against IDENTITY_JWKS) AND authorizes the dev-token mint (INT-0004 §6).
        #  • dev-token — the kernel RUN-token; broker-confined to this dev tenant, used for KERNEL
        #    registry writes (seeds) so they skip the steward-grant/governed-vocab check within the tenant.
        # identity token precedence: explicit AIRAWAT_IDENTITY_TOKEN → a cached `aos login` (browser OIDC,
        # the way to auth against a real Keycloak network) → the local passwordless dev /login.
        self._id_tok = identity_token or self._cached_login() or (self._login(dev_user) if self.idn else None)
        self._dev_tok = token or self._mint_dev_token()
        # PLANE BOUNDARY: deploying to a developer sandbox touches ONLY the kernel (dev-token registry
        # writes) + the app-host (bundle delivery). The NETWORK authority (certify / solutions / install /
        # schema+role governance) is a SHARED, standards-body surface reached ONLY by Marketplace promotion
        # — a tenant-admin action, gated client-side (aos.policy.check_publish) and server-side
        # (PROD_PUBLISH_GATES), done through Studio, never by `aos deploy` from a developer's machine.
        # Inside the dev tenant the dev-token already carries tenant-scoped write (scope:* ; grant + vocab
        # relaxed per INT-0004 / ADR-0051-0106), so no authority round-trip is needed to govern or install.
        self._use_authority = False

    def _cached_login(self):
        try:
            from . import oidc
            return oidc.cached_token()          # config/env-aware issuer resolution
        except Exception:
            return None

    def _login(self, username):
        # Two identity shapes: EC2 wants {username, tenant}; the local realm-based identity wants {username}.
        # Try the tenant-scoped shape first (a bare-{username} server ignores the extra field or errors → we
        # fall back). A `password` is included when the reference IdP has been flipped off passwordless
        # (ADR-0068): supplied via env/config, else — only if the attempt yields nothing on a tty — prompted.
        def _attempt(pw):
            base = {"username": username}
            if pw:
                base["password"] = pw
            bodies = ([{**base, "tenant": self._login_tenant}] if self._login_tenant else []) + [base]
            for body in bodies:
                try:
                    r = self._t("POST", self.idn + "/login", body, None) or {}
                    if r.get("access_token"):
                        return r["access_token"]
                except Exception:
                    continue          # 401 (password required/wrong) or shape mismatch → try the next
            return None
        tok = _attempt(self._password)
        if tok:
            return tok
        # Nothing came back (unknown user, or a flipped IdP needs a password we weren't given). If no
        # password was configured and we're interactive, prompt once and retry.
        if self._password is None and sys.stdin.isatty():
            try:
                import getpass
                pw = getpass.getpass(f"Password for {username}: ") or None
            except Exception:
                pw = None
            if pw:
                self._password = pw
                return _attempt(pw)
        return None

    def _mint_dev_token(self):
        # INT-0004: the kernel mints a dev-token confined to this development tenant. Needs a component_id
        # (any id under the dev tenant — the write capability is tenant-scoped, not component-scoped) and
        # an authenticated Developer bearer (the identity token). The response field is `run_token`.
        cid = f"ds:{self.tenant}:solution/_aos-deploy"
        r = self._t("POST", self.kernel + "/v1/components/dev-token",
                    {"component_id": cid, "tenant": self.tenant}, self._id_tok) or {}
        return r.get("run_token") or r.get("token") or r.get("access_token")

    def _net(self, url, body):
        # NETWORK plane — the shared standards-body authority. A sandbox deploy does NOT reach it
        # (see _use_authority in __init__): the leaf endpoint is logged and the call is skipped, returning
        # an empty ack the callers already treat as "fall back to the dev-scoped local result". The network
        # is exercised only by Marketplace promotion (tenant-admin, via Studio), never by `aos deploy`.
        if not self._use_authority:
            self.log.append(("authority-skip", url.rsplit("/", 1)[-1], "dev-sandbox: kernel+app-host only"))
            return {}
        # PRODUCTION/promotion plane — authenticated by the developer identity token (JWT).
        return self._t("POST", url, body, self._id_tok) or {}

    def _kwrite(self, oid, obj):
        # KERNEL registry plane — a broker write under the dev-token (X-Run-Token header), confined to
        # the dev tenant. scope = the write tenant; the dev-token's capability grants write scope:* there.
        return self._t("POST", self.kernel + "/v1/write",
                       {"scope": self.tenant, "id": oid, "object": obj},
                       None, {"X-Run-Token": self._dev_tok}) or {}

    def govern_schema(self, sid, defn, tenant, environment):
        # ADR-0106 S2: in a dev tenant the network auto-approves this into ds:<tenant>:vocab/<sid>.
        r = self._net(self.net + "/v1/schema-requests", {"schema_id": sid, "definition": defn or {}})
        self.log.append(("govern_schema", sid, r.get("status", "?")))
        return r.get("filed") or _dev_scope(sid, tenant)

    def govern_role(self, role, tenant, environment):
        r = self._net(self.net + "/v1/role-requests", {"role": role, "tenant": tenant})
        self.log.append(("govern_role", role, r.get("status", "?")))

    def build_member(self, ref, tenant, environment, base_dir=None):
        # Dev build: DELIVER the locally-built source bundle to the app-host (so an app's tile appears on the
        # desktop + a driver/engine is materialisable on install), then certify it against the shared catalog
        # ∪ dev vocab (S2) and register it installable-from-source in the dev tenant (INT-0004). ADR-0106.
        cid = _canon_member(ref, tenant)   # `engines/infer` → ds:<tenant>:engine/infer (steward = dev tenant)
        if base_dir and self.app:
            self._publish_bundle(cid, os.path.join(base_dir, ref))
        r = self._net(self.net + "/v1/certify", {"subject_id": cid, "subject_kind": "component", "dev": True})
        self.log.append(("build+certify", cid, "verified" if r.get("certificate") else "?"))

    def _publish_bundle(self, cid, src_dir):
        """Deliver a locally-built component bundle to the app-host (ADR-0106 live-deploy delivery): tar the
        dir and POST it to /bridge/component-publish, which runs the SAME _store_bundle seam Studio uses
        (content-addressed filestore + platform.component_asset + app overlay for an app). DEV-SANDBOX only;
        best-effort — a failure logs and the deploy still governs/installs."""
        import io, tarfile, urllib.request, urllib.parse
        if not os.path.isdir(src_dir):
            self.log.append(("deliver", cid, "no-source")); return None
        kind, ver = "app", "0.1.0"
        mp = os.path.join(src_dir, "meta.yaml")
        if os.path.isfile(mp):
            try:
                import yaml
                m = yaml.safe_load(open(mp).read()) or {}
                kind = m.get("kind", kind); ver = str(m.get("version", ver))
            except Exception:
                pass
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(src_dir, arcname=".")        # arcname "." → extracted dir IS the component dir (meta.yaml at top)
        q = urllib.parse.urlencode({"component": cid, "version": ver, "is_app": "1" if kind == "app" else "0"})
        req = urllib.request.Request(self.app + "/bridge/component-publish?" + q, data=buf.getvalue(), method="POST",
              headers={"Content-Type": "application/gzip", "Authorization": "Bearer " + (self._id_tok or "")})
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=120).read() or b"{}")
            self.log.append(("deliver", cid, ("bundle " + (r.get("file_id") or "")[:16]) if r.get("published") else "no-fid"))
            return r.get("file_id")
        except Exception as e:
            self.log.append(("deliver", cid, "ERR " + str(e)[:90]))
            return None

    def compose_install(self, solution_name, members, tenant, environment):
        sid = f"ds:{tenant}:solution/{solution_name}"
        # ADR-0098 pin: carry each member's pinned version into the SIGNED bundle so the declaration is a
        # reproducible lockfile. Unpinned members serialize as a bare id (resolves latest until the ADR-0098
        # runtime honours the pin at install/materialize).
        mids = []
        for m in members:
            cid, ver = _member_id_ver(m, tenant)
            mids.append({"component_id": cid, "version": ver} if ver else {"component_id": cid})
        # register the signed bundle (network), then install the ADR-0075 closure into the dev tenant —
        # ONE tenant-admin approval grants writes for every member (the developer is admin of their own tenant).
        self._net(self.net + "/v1/solutions",
                  {"solution_id": sid, "title": solution_name, "version": "0.1.0", "members": mids})
        self._net(self.net + "/v1/solutions/install", {"solution_id": sid, "tenant": tenant})
        self.log.append(("compose+install", sid, "ok"))
        return sid

    def seed_viz(self, layer, tenant):
        oid = f"ds:{tenant}:viz/layer/{layer}"
        label = layer.split(".", 1)[-1].replace("_", " ").title()
        self._kwrite(oid, {"schema": "viz.layer", "id": oid,
                           "layer_id": layer, "source_schema": layer, "label": label,
                           "value_field": "value", "color_scale": "viridis"})
        self.log.append(("seed_viz", layer, "ok"))

    def seed_contribution(self, rec, tenant):
        if isinstance(rec, dict) and rec.get("id"):
            self._kwrite(rec["id"], rec)
        self.log.append(("seed_contribution", rec, "ok"))

    def seed_subscription(self, sub, tenant, solution_name="solution"):
        # ADR-0063: a platform.subscription fires a GOVERNED verb on a registry write of `on`'s schema.
        sid, rec = _subscription_record(sub, tenant, solution_name)
        self._kwrite(sid, rec)
        self.log.append(("seed_subscription", f"{sub.get('on')}→{sub.get('do')}", "ok"))

    def open_urls(self, solution_name, tenant):
        return [f"https://…/{tenant}/{solution_name}"]

    def write_project(self, rec):
        # Register (upsert) the Studio project workspace in the dev tenant so `aos`-built solutions appear in
        # Studio (which lists studio.project records) — editable, since build members' source is delivered to
        # filestore by _publish_bundle (Studio's editor loads it via /bridge/component-read). Dev-token write,
        # own-tenant (id prefix ds:<tenant>:) — no authority round-trip.
        self._kwrite(rec["id"], rec)
        self.log.append(("studio-project", rec["id"], f"{len(rec.get('members', []))} members"))

    # ── teardown (ADR-0106 S5) — reverse the closure over the live services, dev-confined ──
    def _kretract(self, oid):
        # KERNEL registry retract (tombstone). Unlike /v1/write (a broker run-token path), retract is
        # attributed via the identity token; the developer (tenant admin of the dev tenant) may tombstone
        # its own tenant's objects (kernel C3: own-tenant only).
        return self._t("POST", self.kernel + "/v1/registry/retract", {"id": oid}, self._id_tok) or {}

    def unseed_viz(self, layer, tenant):
        self._kretract(f"ds:{tenant}:viz/layer/{layer}")
        self.log.append(("unseed_viz", layer, "ok"))

    def unseed_contribution(self, rec, tenant):
        if isinstance(rec, dict) and rec.get("id"):
            self._kretract(rec["id"])
        self.log.append(("unseed_contribution", rec, "ok"))

    def uninstall(self, solution_name, members, tenant):
        # UNINSTALL = REVOKE all of each member's grants in the dev tenant (the inverse of install-as-grant).
        for m in members:
            self._net(self.net + "/v1/grants/revoke-for",
                      {"tenant": tenant, "component_id": _canon_member(m, tenant)})
        self.log.append(("uninstall", solution_name, "ok"))

    def retire_member(self, ref, tenant):
        self._net(self.net + "/v1/certificates/retire", {"subject_id": _canon_member(ref, tenant)})
        self.log.append(("retire", ref, "ok"))

    def retract_schema(self, sid, tenant):
        self._net(self.net + "/v1/schema-requests/retract", {"schema_id": sid})
        self.log.append(("retract_schema", sid, "ok"))

    def retract_role(self, role, tenant):
        # a dev role is governed as a (pending) role-request, not persisted dev-vocab like a schema —
        # nothing durable to retract server-side; recorded for symmetry with the offline backend.
        self.log.append(("retract_role", role, "noop"))

    def unseed_subscription(self, sub, tenant, solution_name="solution"):
        sid, _ = _subscription_record(sub, tenant, solution_name)
        self._kretract(sid)
        self.log.append(("unseed_subscription", f"{sub.get('on')}→{sub.get('do')}", "ok"))


def _urllib_transport(method, url, body, token, headers=None):
    import urllib.request, urllib.error, json as _j, time as _tm
    data = _j.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"}
    if token:
        hdr["Authorization"] = token if str(token).startswith("Bearer ") else "Bearer " + token
    if headers:
        hdr.update(headers)
    last = None
    for a in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=hdr, method=method)
            with urllib.request.urlopen(req, timeout=30) as r:
                return _j.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode()[:300]
            raise RuntimeError(f"{method} {url} → {e.code}: {body_txt}")
        except urllib.error.URLError as e:
            last = e; _tm.sleep(1.5 * (a + 1))
    raise RuntimeError(f"{method} {url} unreachable: {last}")


# ── the pipeline ──────────────────────────────────────────────────────────────
class Deployer:
    def __init__(self, backend):
        self.b = backend

    def deploy_sandbox(self, sol: Solution, tenant):
        """ADR-0106 §2: govern → build+certify → compose+install → seed → open. Idempotent."""
        env = policy.DEV
        rep = {"solution": sol.name, "tenant": tenant, "environment": env, "steps": []}

        # 1) govern (dev: auto-approved in place, tenant-scoped — ADR-0106 §3)
        for s in sol.schemas:
            sid = s.get("id") if isinstance(s, dict) else s
            defn = (s.get("definition") if isinstance(s, dict) else None) or _synth_definition(s)
            gid = self.b.govern_schema(sid, defn, tenant, env)
            rep["steps"].append(("govern-schema", gid))
        for r in sol.roles:
            self.b.govern_role(r, tenant, env)
            rep["steps"].append(("govern-role", r))

        # 2) build + certify the new members (reuse members come from the marketplace)
        for m in sol.build:
            self.b.build_member(m, tenant, env, base_dir=sol.dir)
            rep["steps"].append(("build", m))

        # 3) compose + install the closure into the dev tenant (ADR-0075)
        members = list(sol.reuse) + list(sol.build)
        sid = self.b.compose_install(sol.name, members, tenant, env)
        rep["solution_id"] = sid
        rep["steps"].append(("install", sid))
        # ADR-0098 pin visibility: record resolved pins + flag REUSE members left unpinned (they belong to
        # another steward and will float to latest until the pin is honoured at install/materialize).
        rep["pins"] = {_member_id_ver(m, tenant)[0]: _member_id_ver(m, tenant)[1] for m in members}
        rep["unpinned"] = [_member_id_ver(m, tenant)[0] for m in sol.reuse if not _member_id_ver(m, tenant)[1]]

        # 4) seed viz.layers, contributions, decider wiring
        for layer in sol.viz_layers:
            self.b.seed_viz(layer, tenant)
            rep["steps"].append(("viz", layer))
        for c in sol.contributions:
            self.b.seed_contribution(c, tenant)
            rep["steps"].append(("contribution", c))
        # 4b) wire event-triggers: seed platform.subscription per {on, do} (ADR-0063). The `do` verb must
        # be governed + bound for the dispatcher to fire it; seeding the record itself is unconditional.
        for sub in sol.subscriptions:
            self.b.seed_subscription(sub, tenant, sol.name)
            rep["steps"].append(("subscription", f"{sub.get('on')} → {sub.get('do')}"))

        # 5) stamp "Under Development" + open
        rep["marking"] = policy.dev_marking({"environment": env})
        rep["open"] = self.b.open_urls(sol.name, tenant)
        return rep

    def push_studio_project(self, sol: Solution, tenant):
        """Register (upsert) a studio.project for this solution so it appears in Studio — the bridge between
        the CLI and the in-browser workspace. Members map 1:1 from the manifest: BUILD members are `new`
        (yours to edit + re-publish; their source was delivered to filestore by build_member), REUSE members
        are `existing` (configure, not rebuild). Idempotent: the id is derived from the solution name, so
        re-running updates the same project. Needs the live backend (writes a real registry record)."""
        slug = (re.sub(r"[^a-z0-9]+", "-", str(sol.name).lower()).strip("-") or "project")[:40]
        pid = f"ds:{tenant}:studio/project/{slug}"
        layer_of = {"app": "interaction", "driver": "domain", "engine": "intelligence",
                    "skill": "intelligence", "pipeline": "intelligence", "agent": "intelligence",
                    "adapter": "domain", "provider": "domain"}

        def _member(ref, origin):
            cid = _canon_member(ref, tenant)
            kind = cid.split(":", 2)[2].split("/", 1)[0] if cid.startswith("ds:") else "engine"
            title = cid.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").title()
            return {"ref": cid, "kind": kind, "title": title, "origin": origin,
                    "status": "published", "layer": layer_of.get(kind, "domain"), "reads": [], "writes": []}

        members = [_member(m, "new") for m in sol.build] + [_member(m, "existing") for m in sol.reuse]
        rec = {"schema": "studio.project", "id": pid, "tenant": tenant,
               "title": str(sol.name).replace("-", " ").title(),
               "goal": sol.d.get("goal") or "", "brief": {}, "status": "active", "members": members}
        self.b.write_project(rec)
        return {"id": pid, "members": len(members),
                "build": [m["ref"] for m in members if m["origin"] == "new"]}

    def destroy_sandbox(self, sol: Solution, tenant):
        """ADR-0106 §S5: tear the sandbox closure down in REVERSE — unseed → uninstall (revoke grants) →
        decertify build members → retract dev vocab. Idempotent + best-effort: a step whose target is
        already gone is recorded 'skip', never fatal, so destroy is safe to re-run. Confined to the dev
        tenant throughout (dev-token for kernel retracts, tenant-admin identity for network revokes)."""
        env = policy.DEV
        rep = {"solution": sol.name, "tenant": tenant, "environment": env, "steps": []}

        def step(label, fn, arg):
            try:
                fn(arg, tenant); rep["steps"].append((label, arg, "ok"))
            except Exception as e:
                rep["steps"].append((label, arg, f"skip ({str(e)[:60]})"))

        # 1) unseed subscriptions + contributions + viz.layers (retract the tombstone)
        for sub in sol.subscriptions:
            try:
                self.b.unseed_subscription(sub, tenant, sol.name); rep["steps"].append(("unseed-subscription", f"{sub.get('on')}→{sub.get('do')}", "ok"))
            except Exception as e:
                rep["steps"].append(("unseed-subscription", f"{sub.get('on')}→{sub.get('do')}", f"skip ({str(e)[:50]})"))
        for c in sol.contributions:
            step("unseed-contribution", self.b.unseed_contribution, c)
        for layer in sol.viz_layers:
            step("unseed-viz", self.b.unseed_viz, layer)
        # 2) uninstall — revoke every member's grants (the inverse of the ADR-0075 install closure)
        members = list(sol.reuse) + list(sol.build)
        try:
            self.b.uninstall(sol.name, members, tenant); rep["steps"].append(("uninstall", sol.name, "ok"))
        except Exception as e:
            rep["steps"].append(("uninstall", sol.name, f"skip ({str(e)[:60]})"))
        # 3) decertify the build members (dev certs)
        for m in sol.build:
            step("retire", self.b.retire_member, m)
        # 4) retract the dev-governed vocabulary (leaves the dev tenant clean for a fresh deploy)
        for s in sol.schemas:
            sid = s.get("id") if isinstance(s, dict) else s
            step("retract-schema", self.b.retract_schema, sid)
        for r in sol.roles:
            step("retract-role", self.b.retract_role, r)
        rep["destroyed"] = f"ds:{tenant}:solution/{sol.name}"
        return rep


def _dev_scope(schema_id, tenant):
    """Dev-governed vocabulary is quarantined to the dev tenant's namespace (ADR-0106 §3)."""
    return f"ds:{tenant}:vocab/{schema_id}"


def _sub_slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-") or "x"


def _subscription_record(sub, tenant, sol_name):
    """Build a governed platform.subscription from a solution.yaml entry `{on, do, where?}`.
    `on` is the trigger schema (a registry write of it fires the sub); `do` is the governed verb
    to run (must be governed + bound). Shape mirrors app_host._materialize_subscriptions."""
    # YAML 1.1 gotcha: a bare `on:` key is parsed as the boolean True by pyyaml (but stays "on" under the
    # inline fallback) — accept both so `{on: …, do: …}` works in every parser.
    on = sub.get("on") or sub.get(True)
    do = sub.get("do") or sub.get("action")
    where = sub.get("where")
    sid = f"ds:{tenant}:subscription/{_sub_slug(sol_name)}--{_sub_slug(on)}"
    rec = {"schema": "platform.subscription", "id": sid, "tenant": tenant,
           "name": f"{sol_name}: {on} → {do}", "enabled": True,
           "match": dict({"subject_schema": on}, **({"where": where} if where else {})),
           "action": {"kind": "http", "interface": do,
                      "body": {"tenant": tenant, "reason": f"{on} → {do} (solution {sol_name})"}}}
    return sid, rec


_VER = re.compile(r"^\d+\.\d+\.\d+$")


def _split_ver(ref):
    """Split a possibly-pinned member ref into (base, version|None). Accepts `name@1.2.3`,
    `engines/infer@1.2.3`, `ds:t:engine/x@1.2.3` — the version is the trailing `@x.y.z`."""
    s = str(ref)
    if "@" in s:
        base, ver = s.rsplit("@", 1)
        if _VER.match(ver):
            return base, ver
    return s, None


def _canon_member(ref, tenant):
    """Canonicalize a member reference to a dev-scoped component id the network can steward-check:
    `engines/infer` → `ds:<tenant>:engine/infer`; a pinned `lifecycle-engine@0.1.0` → the bare cid
    (the version is carried separately, see _member_id_ver). An already-qualified `ds:…` id passes
    through. The kind dir is singularized (`engines`→`engine`, `apps`→`app`)."""
    ref, _ = _split_ver(ref)
    if isinstance(ref, str) and ref.startswith("ds:"):
        return ref
    parts = str(ref).strip("/").split("/")
    name = parts[-1]
    kind = parts[-2] if len(parts) > 1 else "engine"
    kind = {"engines": "engine", "drivers": "driver", "apps": "app", "solutions": "solution",
            "pipelines": "pipeline", "agents": "agent"}.get(kind, kind.rstrip("s") or "engine")
    return f"ds:{tenant}:{kind}/{name}"


def _member_id_ver(ref, tenant):
    """(canonical component id, pinned version | None) for a manifest member — ADR-0098 pin.
    The version, when present, travels into the signed solution bundle so the declaration is a
    reproducible lockfile (runtime honoring of the pin is the separate ADR-0098 install work)."""
    _, ver = _split_ver(ref)
    return _canon_member(ref, tenant), ver


def _synth_definition(entry):
    """Synthesize a house-rule-conformant JSON-Schema from the manifest shorthand
    (`{id, x-kind, x-view?, fields?}`) — the "dev supplies only the domain delta" principle:
    the SDK fills the fixed envelope (schema+id), closure (additionalProperties:false), the
    resolvable-id pattern, and — for a measure — the (observed_at, prov) provenance pair + the
    entity it views (x-view). A full `definition` in the manifest always overrides this."""
    if not isinstance(entry, dict):
        entry = {"id": entry}
    sid = entry.get("id", "")
    kind = entry.get("x-kind") or entry.get("kind") or "measure"
    props = {"schema": {"type": "string"},
             "id": {"type": "string", "pattern": r"^ds:[a-z0-9-]+:\S+$"}}
    required = ["schema", "id"]
    defn = {"type": "object", "x-kind": kind, "additionalProperties": False}
    if kind == "measure":
        # a measure views an entity (x-view) and carries provenance + observation time (R5)
        defn["x-view"] = entry.get("x-view") or ("space.cell" if sid.startswith("cell.") else "space.cell")
        props["observed_at"] = {"type": "string"}
        props["prov"] = {"type": "object"}
        props["cell_id"] = {"type": "string"}
        props["value"] = {"type": "number"}
        required += ["observed_at", "prov"]
    # caller-declared extra fields (shorthand: {fields: {name: type}})
    for fname, ftype in (entry.get("fields") or {}).items():
        props[fname] = {"type": ftype if isinstance(ftype, str) else "string"}
    defn["properties"] = props
    defn["required"] = required
    return defn


# ── YAML reader: PyYAML if present, else an inline-only fallback ──────────────
def _load_yaml(text):
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except ImportError:
        return _inline_yaml(text)


def _inline_yaml(text):
    """Zero-dependency YAML-subset parser for solution.yaml when PyYAML is absent. Handles the common
    block + inline forms: nested maps by indentation, block `- ` lists (of scalars, inline [..]/{..},
    or multi-line maps), and inline [lists]/{maps}. Not a full YAML parser — no anchors/multiline
    scalars/flow-over-lines — but enough that an author never has to hand-flatten to inline."""
    lines = []
    for raw in text.splitlines():
        s = raw.split(" #", 1)[0].rstrip()
        if not s.strip() or s.lstrip().startswith("#"):
            continue
        lines.append((len(s) - len(s.lstrip()), s.strip()))
    val, _ = _parse_yaml_block(lines, 0, -1)
    return val if isinstance(val, dict) else (val or {})


def _parse_yaml_block(lines, i, parent_indent):
    """Parse the block of lines indented deeper than `parent_indent`, starting at index `i`.
    A block is a LIST if its first line starts with `-`, else a MAP. Returns (value, next_index)."""
    n = len(lines)
    if i >= n or lines[i][0] <= parent_indent:
        return {}, i
    indent = lines[i][0]
    container = [] if lines[i][1].startswith("-") else {}
    while i < n:
        ind, text = lines[i]
        if ind < indent:
            break
        if ind > indent:                       # stray deeper line with no owning key — skip defensively
            i += 1; continue
        if text.startswith("-"):
            item = text[1:].strip()
            if item == "":                     # "-" then a nested block on the following lines
                child, i = _parse_yaml_block(lines, i + 1, indent)
                container.append(child)
            elif ":" in item and not item.startswith(("[", "{")):
                # a map list item: first key is on the "- " line; more keys follow, indented deeper
                k, _, v = item.partition(":")
                m = {k.strip(): (_scalar(v.strip()) if v.strip() else {})}
                i += 1
                rest, i = _parse_yaml_block(lines, i, indent)
                if isinstance(rest, dict):
                    m.update(rest)
                container.append(m)
            else:
                container.append(_scalar(item)); i += 1
        else:
            k, _, v = text.partition(":")
            k, v = k.strip(), v.strip()
            if v == "":                        # nested block (map or list) on the following lines
                child, i = _parse_yaml_block(lines, i + 1, indent)
                container[k] = child
            else:
                container[k] = _scalar(v); i += 1
    return container, i


def _scalar(v):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_scalar(x) for x in _split_top(inner)]
    if v.startswith("{") and v.endswith("}"):
        d = {}
        for part in _split_top(v[1:-1]):
            if ":" in part:
                kk, vv = part.split(":", 1)
                d[kk.strip()] = _scalar(vv.strip())
        return d
    if v in ("true", "True"): return True
    if v in ("false", "False"): return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v.strip('"').strip("'")


def _split_top(s):
    """Split on commas not inside {} or []."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [x.strip() for x in out]
