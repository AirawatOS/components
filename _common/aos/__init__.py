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
import os, json, time, base64, hashlib, contextvars, urllib.request, urllib.error

__all__ = ["engine", "driver", "ctx", "Ctx", "Reject", "now"]

# ── observed vocabulary (populated at runtime by Ctx; consumed by aos.meta.derive) ──
# READS/WRITES → scopes; FILES (put/get) → the `filestore` capability the runtime must grant.
READS, WRITES, FILES = set(), set(), set()


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
                 kfind=None, kwrite=None, kget=None, ai=None, call=None, kbatch=None,
                 fput=None, fget=None):
        self.body = body or {}
        self.tenant = tenant
        self.sub = sub
        self.claims = claims or {}
        self._kfind, self._kwrite, self._kget = kfind, kwrite, kget
        self._ai, self._call, self._kbatch = ai, call, kbatch
        self._fput, self._fget = fput, fget

    # --- adapters ---------------------------------------------------------------
    @classmethod
    def from_host(cls, hc):
        """In-process HANDLERS engine: wrap app_host's injected ctx dict."""
        return cls(body=hc.get("body"), tenant=hc.get("tenant"), sub=hc.get("sub"),
                   claims=hc.get("claims"),
                   kfind=lambda s: hc["kfind"](s), kwrite=lambda i, o: hc["kwrite"](i, o),
                   kget=lambda i, _s=None: hc.get("kget", lambda _x: None)(i),
                   ai=hc.get("ai_complete"), call=hc.get("call"),
                   fput=hc.get("file_put"), fget=hc.get("file_get"))

    @classmethod
    def in_memory(cls, store=None, tenant="you-dev", body=None):
        """A dict-backed ctx for `aos test` — the whole surface (facts/get/write/write_many)
        against an in-process registry, no live services. Same code path a driver runs live."""
        store = store if store is not None else {}
        blobs = {}                                  # content-addressed blob store — same seam a driver runs live
        def _fput(b):
            fid = "sha2-256:" + hashlib.sha256(b).hexdigest()   # digest IS the id (write-once + dedup)
            blobs[fid] = b
            return fid
        cx = cls(body=body, tenant=tenant,
                 kfind=lambda s: [o for o in store.values() if o.get("schema") == s],
                 kwrite=lambda i, o: store.__setitem__(i, o),
                 kget=lambda i, s=None: store.get(i),
                 kbatch=lambda objs: [store.__setitem__(o["id"], o) for o in objs],
                 fput=_fput, fget=lambda i: blobs.get(i))
        cx._store = store
        cx._blobs = blobs
        return cx

    @classmethod
    def from_broker(cls, body=None):
        """Headless driver/engine: the broker transport (RUN_TOKEN + AIRAWAT_API),
        owned once here instead of hand-rolled per component (retry + backoff included)."""
        api, tok = os.environ.get("AIRAWAT_API", ""), os.environ.get("RUN_TOKEN", "")
        hdr = {"Content-Type": "application/json", "X-Run-Token": tok}

        def _req(method, path, payload=None, tries=4):
            data = json.dumps(payload).encode() if payload is not None else None
            last = None
            for a in range(tries):
                try:
                    req = urllib.request.Request(api + path, data=data, headers=hdr, method=method)
                    with urllib.request.urlopen(req, timeout=90) as r:
                        return json.loads(r.read() or b"{}")
                except urllib.error.HTTPError as e:
                    if e.code in (502, 503, 504) and a < tries - 1:
                        time.sleep(1.5 * (a + 1)); last = e; continue
                    raise Reject(f"broker {method} {path} → {e.code}: {e.read().decode()[:200]}")
                except urllib.error.URLError as e:
                    last = e; time.sleep(1.5 * (a + 1))
            raise Reject(f"broker {method} {path} failed: {last}")

        return _broker_ctx(_req, body)

    # --- the API developers use -------------------------------------------------
    def facts(self, schema, where=None):
        READS.add(schema)
        rows = self._kfind(schema) or []
        q = Query(rows, schema)
        return q.where(where) if where else q

    def get(self, oid, scope=None):
        if not self._kget:
            return None
        sc = scope or _scope_from_id(oid)
        if sc:
            READS.add(sc)                          # a point read still needs read capability on the scope
        try:
            return self._kget(oid, sc)             # broker kget takes (id, scope)
        except TypeError:
            return self._kget(oid)                 # host kget takes (id)

    def write(self, obj=None, *, schema=None, id=None, **fields):
        if obj is None:
            obj = dict(fields, schema=schema, id=id)
        sch = obj.get("schema")
        if sch:
            WRITES.add(sch)
        obj.setdefault("tenant", self.tenant)
        self._kwrite(obj["id"], obj)
        return obj

    def write_many(self, objs):
        """Bulk write — the driver superpower. One batch round-trip over the broker (/v1/write/batch)
        when headless; a simple loop in-process. Every object is tenant-stamped + its schema tracked."""
        objs = [dict(o) for o in objs]
        for o in objs:
            if o.get("schema"):
                WRITES.add(o["schema"])
            o.setdefault("tenant", self.tenant)
        if self._kbatch:
            self._kbatch(objs)
        else:
            for o in objs:
                self._kwrite(o["id"], o)
        return len(objs)

    @property
    def file(self):
        """The filestore surface — `ctx.file.put(bytes|str) → id`, `ctx.file.get(id) → bytes`.
        Content-addressed (the id IS the sha2-256 digest): write-once + dedup, and possessing the
        digest is the read capability. Same code path host (local blob), broker (/v1/file), and
        `aos test` (in-memory). Using it derives the `filestore` capability into meta."""
        return _FileAPI(self)

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


class _FileAPI:
    """`ctx.file` — the content-addressed blob surface. Bound per-invocation to the runtime's
    put/get callables (host local-blob / broker /v1/file / in-memory). Every call records the
    `filestore` capability so `aos check` derives it into meta (never hand-declared)."""
    def __init__(self, cx):
        self._cx = cx

    def put(self, data):
        """Store bytes (or a str, encoded utf-8) → returns the content-addressed id (the sha2-256 digest)."""
        FILES.add("put")
        if self._cx._fput is None:
            raise Reject("ctx.file.put unavailable in this runtime (no filestore capability)")
        return self._cx._fput(data.encode() if isinstance(data, str) else bytes(data))

    def get(self, fid):
        """Fetch bytes by content-addressed id. Returns raw bytes (b'' if absent)."""
        FILES.add("get")
        if self._cx._fget is None:
            raise Reject("ctx.file.get unavailable in this runtime (no filestore capability)")
        return self._cx._fget(fid)


def _slug(s):
    import re
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:48] or "x"


def _scope_from_id(oid):
    """AirawatOS id convention `ds:<tenant>:<schema>/<localid>` → the schema/scope segment."""
    try:
        return str(oid).split(":", 2)[2].split("/", 1)[0]
    except Exception:
        return None


def _broker_ctx(_req, body):
    """Ctx backed by the real broker HTTP verbs (headless) — the exact kernel contract:
    GET /v1/context · POST /v1/query (scope+keyset paging) · /v1/read (scope) · /v1/write ·
    /v1/write/batch · /v1/complete. This is the full engine surface, over the broker."""
    def kfind(scope):
        rows, cursor = [], None
        while True:
            r = _req("POST", "/v1/query", {"scope": scope, "limit": 5000, **({"cursor": cursor} if cursor else {})}) or {}
            rows.extend(r.get("results", []))
            cursor = r.get("next_cursor")
            if not cursor:
                return rows

    def kwrite(oid, obj):
        return _req("POST", "/v1/write", {"scope": obj.get("schema") or _scope_from_id(oid), "id": oid, "object": obj})

    def kbatch(objs):
        items = [{"scope": o.get("schema") or _scope_from_id(o["id"]), "id": o["id"], "object": o} for o in objs]
        return _req("POST", "/v1/write/batch", {"items": items})

    def kget(oid, scope=None):
        return (_req("POST", "/v1/read", {"scope": scope or _scope_from_id(oid), "id": oid}) or {}).get("object")

    def ai(messages, max_tokens=900):
        r = _req("POST", "/v1/complete", {"messages": messages, "max_tokens": max_tokens}) or {}
        return r.get("content", r)

    def fput(b):    # broker filestore PUT (PLAT-FILE-002): base64 over the JSON seam → content-addressed id
        return (_req("POST", "/v1/file/put", {"content_b64": base64.b64encode(b).decode()}) or {}).get("id")

    def fget(fid):  # broker filestore GET: the digest IS the read capability → raw bytes back
        return base64.b64decode((_req("POST", "/v1/file/get", {"id": fid}) or {}).get("content_b64") or "")

    tenant = (_req("GET", "/v1/context") or {}).get("tenant") if body is None else None
    return Ctx(body=body, tenant=tenant, kfind=kfind, kwrite=kwrite, kget=kget, kbatch=kbatch,
               ai=ai, fput=fput, fget=fget)


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

    def run_local(self, store=None, tenant="you-dev"):
        """`aos test` path: run the driver against an in-memory registry (no live services).
        Returns the resulting store so the harness can assert what was written."""
        if not self._main:
            raise Reject("no @driver.main registered")
        cx = Ctx.in_memory(store, tenant)
        token = _current.set(cx)
        try:
            self._main()
        finally:
            _current.reset(token)
        return cx._store

    def registered(self):
        return self._main is not None


driver = _Driver()
