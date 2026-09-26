"""aos.solution — the declarative solution manifest + `aos deploy --sandbox` pipeline (ADR-0106 S1).

`solution.yaml` describes a whole vertical (schemas, roles, workflow graph, reuse+build
members, viz.layers, decider, subscriptions, contributions). The Deployer runs ONE
idempotent pipeline over it — govern → build+certify → compose+install → seed → open —
confined to the developer's tenant, stamping every artifact "Under Development".

The pipeline talks to the platform through a small `Backend` protocol, so it is testable
offline against `InMemoryBackend` and runs for real against `BrokerBackend` (dev-token).
"""
import os, json, re
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

    def build_member(self, ref, tenant, environment):
        first = ref not in self.built
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

    def open_urls(self, solution_name, tenant):
        return [f"https://…/{tenant}/{solution_name}"]


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
            gid = self.b.govern_schema(sid, s.get("definition") if isinstance(s, dict) else None, tenant, env)
            rep["steps"].append(("govern-schema", gid))
        for r in sol.roles:
            self.b.govern_role(r, tenant, env)
            rep["steps"].append(("govern-role", r))

        # 2) build + certify the new members (reuse members come from the marketplace)
        for m in sol.build:
            self.b.build_member(m, tenant, env)
            rep["steps"].append(("build", m))

        # 3) compose + install the closure into the dev tenant (ADR-0075)
        members = list(sol.reuse) + list(sol.build)
        sid = self.b.compose_install(sol.name, members, tenant, env)
        rep["solution_id"] = sid
        rep["steps"].append(("install", sid))

        # 4) seed viz.layers, contributions, decider wiring
        for layer in sol.viz_layers:
            self.b.seed_viz(layer, tenant)
            rep["steps"].append(("viz", layer))
        for c in sol.contributions:
            self.b.seed_contribution(c, tenant)
            rep["steps"].append(("contribution", c))

        # 5) stamp "Under Development" + open
        rep["marking"] = policy.dev_marking({"environment": env})
        rep["open"] = self.b.open_urls(sol.name, tenant)
        return rep


def _dev_scope(schema_id, tenant):
    """Dev-governed vocabulary is quarantined to the dev tenant's namespace (ADR-0106 §3)."""
    return f"ds:{tenant}:vocab/{schema_id}"


# ── YAML reader: PyYAML if present, else an inline-only fallback ──────────────
def _load_yaml(text):
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except ImportError:
        return _inline_yaml(text)


def _inline_yaml(text):
    """Fallback: top-level `key: value` + one level of indented nested maps. Values may be
    scalars or INLINE [lists]/{maps} (block `- ` lists unsupported here — write them inline,
    e.g. `members: {reuse: [a, b]}`). Enough for solution.yaml authored inline."""
    root, cur_key = {}, None
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.strip().startswith("#") or ":" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        k, v = line.strip().split(":", 1)
        k, v = k.strip(), v.strip()
        if indent == 0:
            root[k] = {} if v == "" else _scalar(v)
            cur_key = k if v == "" else None
        elif cur_key is not None and isinstance(root.get(cur_key), dict):
            root[cur_key][k] = _scalar(v) if v else {}
    return root


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
