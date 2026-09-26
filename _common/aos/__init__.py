"""aos — the Airawat build SDK (P1 core prototype).

ONE authoring model for every component kind. The two runtimes that exist today —
in-process HANDLERS engines (rich ctx dict injected by app_host) and headless main()
drivers/engines (only RUN_TOKEN + AIRAWAT_API env, hand-rolled urllib) — are unified
behind a single `ctx` object and a set of decorators. The developer writes logic; the
SDK owns the broker transport, the (result, err) convention, HANDLERS assembly, and
reads/writes tracking (so meta.yaml scopes are derived, not hand-maintained).

    from aos import engine, ctx

    @engine.verb("assess-effectiveness", implements="intelligence.assess_effectiveness@0.1.0")
    def assess(body):
        decs = ctx.facts("governance.decision").where(lambda d: d.get("outcome") == "approved")
        ...
        return ctx.write(obj)          # obj OR (result, err) OR raise aos.Reject(...)

    HANDLERS = engine.handlers()       # host-compatible export; app_host finds it unchanged

Design notes:
- `ctx` is a context-local proxy (contextvars) bound per invocation — no ctx plumbing.
- `ctx.facts(schema)` records a READ; `ctx.write(obj)` records a WRITE. `aos check`
  dry-runs each verb against a mock registry to derive `reads`/`writes` → `scopes`.
- Backed by the injected host ctx (in-process) OR the broker HTTP transport (headless);
  the developer's code is identical either way.
"""
import os, json, time, contextvars, urllib.request, urllib.error

__all__ = ["engine", "driver", "ctx", "Ctx", "Reject", "now"]

# ── observed vocabulary (populated at runtime by Ctx; consumed by aos.meta.derive) ──
READS, WRITES = set(), set()


class Reject(Exception):
    """Raise to return a clean (None, message) to the host instead of a crash."""


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ─────────────────────────────────────────────────────────────────────────────
# Query — a small list-like wrapper over fetched records with fluent filters.
# Every fetch records the schema as a READ (drives scope derivation).
# ─────────────────────────────────────────────────────────────────────────────
class Query:
    def __init__(self, rows, schema=None):
        self._rows = list(rows)
        self._schema = schema

    def where(self, pred):
        return Query([r for r in self._rows if pred(r)], self._schema)

    def recent(self, field="observed_at", n=None):
        rows = sorted(self._rows, key=lambda r: r.get(field) or "", reverse=True)
        return Query(rows[:n] if n else rows, self._schema)

    def by_id(self):
        return {r.get("id"): r for r in self._rows}

    def list(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)

    def __len__(self):
        return len(self._rows)

    def __getitem__(self, i):
        return self._rows[i]


# ─────────────────────────────────────────────────────────────────────────────
# Ctx — the one surface. Wraps the host-injected dict OR the broker HTTP transport.
# ─────────────────────────────────────────────────────────────────────────────
class Ctx:
    def __init__(self, *, body=None, tenant=None, sub=None, claims=None,
                 kfind=None, kwrite=None, kget=None, ai=None, call=None):
        self.body = body or {}
        self.tenant = tenant
        self.sub = sub
        self.claims = claims or {}
        self._kfind, self._kwrite, self._kget = kfind, kwrite, kget
        self._ai, self._call = ai, call

    # --- adapters ---------------------------------------------------------------
    @classmethod
    def from_host(cls, hc):
        """In-process HANDLERS engine: wrap app_host's injected ctx dict."""
        return cls(body=hc.get("body"), tenant=hc.get("tenant"), sub=hc.get("sub"),
                   claims=hc.get("claims"),
                   kfind=lambda s: hc["kfind"](s), kwrite=lambda i, o: hc["kwrite"](i, o),
                   kget=lambda i: hc.get("kget", lambda _x: None)(i),
                   ai=hc.get("ai_complete"), call=hc.get("call"))

    @classmethod
    def from_broker(cls, body=None):
        """Headless driver/engine: the broker transport (RUN_TOKEN + AIRAWAT_API),
        owned once here instead of hand-rolled per component (retry + backoff included)."""
        api, tok = os.environ.get("AIRAWAT_API", ""), os.environ.get("RUN_TOKEN", "")

        def _post(path, payload, tries=4):
            data = json.dumps(payload).encode()
            hdr = {"Content-Type": "application/json", "X-Run-Token": tok}
            last = None
            for a in range(tries):
                try:
                    req = urllib.request.Request(api + path, data=data, headers=hdr, method="POST")
                    with urllib.request.urlopen(req, timeout=30) as r:
                        return json.loads(r.read() or b"{}")
                except (urllib.error.HTTPError, urllib.error.URLError) as e:
                    last = e
                    time.sleep(1.5 * (a + 1))
            raise Reject(f"broker call {path} failed: {last}")

        cx = _broker_ctx(_post, body)
        return cx

    # --- the API developers use -------------------------------------------------
    def facts(self, schema, where=None):
        READS.add(schema)
        rows = self._kfind(schema) or []
        q = Query(rows, schema)
        return q.where(where) if where else q

    def get(self, oid):
        return self._kget(oid) if self._kget else None

    def write(self, obj=None, *, schema=None, id=None, **fields):
        if obj is None:
            obj = dict(fields, schema=schema, id=id)
        sch = obj.get("schema")
        if sch:
            WRITES.add(sch)
        obj.setdefault("tenant", self.tenant)
        self._kwrite(obj["id"], obj)
        return obj

    def ai(self, messages, max_tokens=900):
        if not self._ai:
            raise Reject("ai() unavailable in this runtime")
        return self._ai(messages, max_tokens)

    def call(self, engine_id, verb, payload):
        if not self._call:
            raise Reject("call() unavailable in this runtime")
        return self._call(engine_id, verb, payload)

    def now(self):
        return now()

    # ── spine sugar (P2): the decision/service spine as API, not rediscovered shapes ──
    def recommend(self, title, action, evidence, rationale="", confidence=0.7, produced_by=None, id=None):
        """Write a governance.recommendation that cites its evidence (ADR-0029). Returns the record."""
        rid = id or f"ds:{self.tenant}:governance/recommendation/{_slug(title)}-{now()[:19]}"
        return self.write({"schema": "governance.recommendation", "id": rid, "title": title,
                           "proposed_action": action, "rationale": rationale,
                           "evidence": list(evidence or []), "method": "rule", "confidence": confidence,
                           "produced_by": produced_by, "observed_at": now()})

    def route_to(self, role, title, ref=None, body="", case=None, scope=None, actions=None, verb="review"):
        """Open a task + a role-addressed inbox item — the fan-out step every vertical repeats."""
        base = f"ds:{self.tenant}"
        tid = f"{base}/task/{_slug(title)}-{now()[11:19].replace(':','')}"
        actor = role if str(role).startswith("role://") else f"role://{self.tenant}/{role}"
        self.write({"schema": "platform.task", "id": tid, "case": case, "title": title, "verb": verb,
                    "assignee": actor, "status": "open", "created_at": now(), "scope": scope})
        iid = f"{base}/platform/inbox_item/{_slug(title)}-{now()[11:19].replace(':','')}"
        self.write({"schema": "platform.inbox_item", "id": iid, "actor": actor, "title": title,
                    "body": body, "ref": ref or tid, "kind": "task", "status": "unread",
                    "created_at": now(), "scope": scope, **({"actions": actions} if actions else {})})
        return {"task": tid, "inbox_item": iid, "routed_to": actor}

    def decide(self, recommendation_ids, authority, outcome="approved", rationale="", id=None):
        """Admit recommendation(s) into a governance.decision. Signing is a host capability
        (decision-service / Vault) — used when available; the record shape is fixed here."""
        did = id or f"ds:{self.tenant}:governance/decision/{now()[:19].replace(':','')}"
        core = {"schema": "governance.decision", "id": did, "outcome": outcome, "authority": authority,
                "rationale": rationale, "evidence_manifest": list(recommendation_ids or []), "decided_at": now()}
        if self._call:
            try:
                signed = self._call("ds:airawatdeveloper:engine/decision-service", "sign-decision", core)
                if isinstance(signed, dict) and signed.get("signature"):
                    core = signed
            except Exception:
                pass
        return self.write(core)

    def open_case(self, workflow, payload):
        """Open a platform.workflow graph case via the one interpreter (ADR-0097)."""
        return self.call("ds:airawatdeveloper:engine/lifecycle-engine", "lifecycle-request",
                         {"workflow": workflow, "payload": payload})


def _slug(s):
    import re
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:48] or "x"


def _broker_ctx(_post, body):
    """Ctx backed by broker HTTP verbs (headless). Kept minimal for P1."""
    def kfind(schema):
        return (_post("/v1/query", {"filter": {"schema": schema}, "limit": 100000}) or {}).get("results", [])

    def kwrite(oid, obj):
        return _post("/v1/write", {"scope": obj.get("schema"), "id": oid, "object": obj})

    def kget(oid):
        r = (_post("/v1/read", {"id": oid}) or {}).get("results", [])
        return r[0].get("object") if r else None

    tenant = (_post("/v1/context", {}) or {}).get("tenant") if body is None else None
    return Ctx(body=body, tenant=tenant, kfind=kfind, kwrite=kwrite, kget=kget)


# ─────────────────────────────────────────────────────────────────────────────
# ctx — a context-local proxy so handlers read `ctx.facts(...)` with no plumbing.
# ─────────────────────────────────────────────────────────────────────────────
_current = contextvars.ContextVar("aos_ctx", default=None)


class _CtxProxy:
    def __getattr__(self, name):
        cur = _current.get()
        if cur is None:
            raise RuntimeError("aos.ctx used outside a handler invocation")
        return getattr(cur, name)


ctx = _CtxProxy()


# ─────────────────────────────────────────────────────────────────────────────
# engine — verb registry + host-compatible HANDLERS assembly.
# ─────────────────────────────────────────────────────────────────────────────
class _Engine:
    def __init__(self):
        self._verbs = {}          # verb-name -> {"fn":.., "implements":.., "reads":set, "writes":set}

    def verb(self, name, implements=None, reads=None, writes=None):
        def deco(fn):
            self._verbs[name] = {"fn": fn, "implements": implements,
                                 "reads": set(reads or []), "writes": set(writes or [])}
            for r in (reads or []):
                READS.add(r)
            for w in (writes or []):
                WRITES.add(w)
            return fn
        return deco

    def handlers(self):
        """Return the app_host-compatible HANDLERS dict: verb -> h(host_ctx)->(result,err).
        The dispatch, ctx binding, (result,err) convention and error trapping live here,
        so the developer's function is a plain `def f(body): ... return result`."""
        out = {}
        for name, spec in self._verbs.items():
            out[name] = self._wrap(spec["fn"])
        return out

    @staticmethod
    def _wrap(fn):
        def h(host_ctx):
            cx = Ctx.from_host(host_ctx)
            token = _current.set(cx)
            try:
                res = fn(cx.body)
                if isinstance(res, tuple) and len(res) == 2:
                    return res
                return res, None
            except Reject as e:
                return None, str(e)
            except Exception as e:
                return None, f"{type(e).__name__}: {e}"
            finally:
                _current.reset(token)
        return h

    # introspection for aos.meta / aos check
    def verbs(self):
        return dict(self._verbs)


engine = _Engine()


# ─────────────────────────────────────────────────────────────────────────────
# driver — headless entrypoint decorators (P1: minimal, same ctx surface).
# ─────────────────────────────────────────────────────────────────────────────
class _Driver:
    def __init__(self):
        self._main = None
        self.schedule = None

    def main(self, fn):
        self._main = fn
        return fn

    def scheduled(self, cron):
        self.schedule = cron

        def deco(fn):
            self._main = fn
            return fn
        return deco

    def run(self):
        """Called from `if __name__ == '__main__'`. Builds the broker-backed ctx once."""
        if not self._main:
            raise Reject("no @driver.main registered")
        cx = Ctx.from_broker()
        token = _current.set(cx)
        try:
            return self._main()
        finally:
            _current.reset(token)


driver = _Driver()
