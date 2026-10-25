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

__all__ = ["engine", "driver", "connector", "ConnectorQuery", "ctx", "Ctx", "Reject", "now", "egress", "ingest"]

# ── observed vocabulary (populated at runtime by Ctx; consumed by aos.meta.derive) ──
# READS/WRITES → scopes; FILES (put/get) → the meta `files:` declaration; EGRESS → external hosts reached via
# ctx.fetch (observed); SECRETS → secret names injected via ctx.fetch (observed).
READS, WRITES, FILES, EGRESS, SECRETS = set(), set(), set(), set(), set()

# Declared egress policy (module-top `aos.egress(...)`, seen at import so `aos check` derives it): host →
# {rate_per_min, user_agent, secret, provides, source_ref}. This is what makes meta.egress/secrets/sources a
# function of the CODE (retiring the hand-maintained YAML blocks), the same way reads/writes derive scopes.
_EGRESS_DECL = {}


def egress(host, *, rate_per_min=None, user_agent=None, secret=None, provides=None, source_ref=None):
    """Declare an external host this component egresses to (via ctx.fetch), once at module top. The broker
    still enforces the real allowlist/rate/secret server-side; this declaration lets `aos` DERIVE the meta
    egress/secrets/sources blocks from code instead of hand-maintaining them. Returns the host."""
    _EGRESS_DECL[host] = {k: v for k, v in {"rate_per_min": rate_per_min, "user_agent": user_agent,
                                            "secret": secret, "provides": provides, "source_ref": source_ref}.items()
                          if v is not None}
    if secret:
        SECRETS.add(secret)
    return host


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
                 fput=None, fget=None, ffetch=None, caps=None):
        self.body = body or {}
        self.tenant = tenant
        self.sub = sub
        self.claims = claims or {}
        self._kfind, self._kwrite, self._kget = kfind, kwrite, kget
        self._ai, self._call, self._kbatch = ai, call, kbatch
        self._fput, self._fget, self._ffetch = fput, fget, ffetch
        self._caps = caps or {}          # host-only capabilities (kfind_x, http, network_url, embed, …)

    # --- adapters ---------------------------------------------------------------
    @classmethod
    def from_host(cls, hc):
        """In-process HANDLERS engine: wrap app_host's injected ctx dict."""
        return cls(body=hc.get("body"), tenant=hc.get("tenant"), sub=hc.get("sub"),
                   claims=hc.get("claims"),
                   kfind=lambda s: hc["kfind"](s), kwrite=_host_kwrite(hc["kwrite"]),
                   kget=lambda i, _s=None: hc.get("kget", lambda _x: None)(i),
                   ai=hc.get("ai_complete"), call=hc.get("call"),
                   fput=hc.get("file_put"), fget=hc.get("file_get"), ffetch=hc.get("fetch"),
                   caps={k: hc[k] for k in ("kfind_x", "http", "network_url", "kernel_url", "embed",
                                            "embed_url", "ensure_install", "federate", "remote_status",
                                            "provision_login", "token") if k in hc})

    @classmethod
    def in_memory(cls, store=None, tenant="you-dev", body=None, fetches=None):
        """A dict-backed ctx for `aos test` — the whole surface (facts/get/write/write_many/file)
        against an in-process registry, no live services. Same code path a driver runs live.
        `fetches` supplies egress responses offline: a dict url→{status,body,headers} or a callable
        (url, method, body, inject)→that dict — so a fetch driver is testable without network or fabrication."""
        store = store if store is not None else {}
        blobs = {}                                  # content-addressed blob store — same seam a driver runs live
        def _fput(b):
            fid = "sha2-256:" + hashlib.sha256(b).hexdigest()   # digest IS the id (write-once + dedup)
            blobs[fid] = b
            return fid
        def _ffetch(url, method, body_, inject, headers):
            r = fetches(url, method, body_, inject) if callable(fetches) else (fetches or {}).get(url)
            if r is None:
                raise Reject(f"ctx.fetch: no offline fixture for {url} (seed Ctx.in_memory(fetches=...) in your test)")
            return r
        cx = cls(body=body, tenant=tenant,
                 kfind=lambda s: [o for o in store.values() if o.get("schema") == s],
                 kwrite=lambda i, o, **_env: store.__setitem__(i, o),
                 kget=lambda i, s=None: store.get(i),
                 kbatch=lambda entries, asserted_at=None: [store.__setitem__(o["id"], o) for o, _vt in entries],
                 fput=_fput, fget=lambda i: blobs.get(i), ffetch=_ffetch,
                 caps={"kfind_x": lambda schema, target, where=None:      # cross-tenant read, testable offline
                       [o for o in store.values() if o.get("schema") == schema and o.get("tenant") == target]})
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

    def write(self, obj=None, *, schema=None, id=None,
              valid_time=None, valid_from=None, valid_to=None, asserted_at=None, **fields):
        """Write a record. Pass valid_time={"from":…,"to"?:…} (or the valid_from/valid_to shorthands) to
        record it as a bitemporal ASSERTION true over that world-time window — the correct shape for an
        observation whose validity is pinned to when it was measured, not when it was written. asserted_at
        sets the transaction time (when the system learned it). Omit both → a latest-wins ENTITY, as before."""
        if obj is None:
            obj = dict(fields, schema=schema, id=id)
        sch = obj.get("schema")
        if sch:
            WRITES.add(sch)
        obj.setdefault("tenant", self.tenant)
        self._kwrite(obj["id"], obj, valid_time=(valid_time or _valid_period(valid_from, valid_to)),
                     asserted_at=asserted_at)
        return obj

    def write_many(self, objs, *, asserted_at=None, valid_from_field=None):
        """Bulk write — the driver superpower. One batch round-trip over the broker (/v1/write/batch)
        when headless; a simple loop in-process. Every object is tenant-stamped + its schema tracked.
        valid_from_field names a per-object field (e.g. "observed_at") to lift into each record's valid_time
        window — so a time-series backfill stays bitemporally correct row-by-row. asserted_at is one
        transaction time for the whole batch."""
        objs = [dict(o) for o in objs]
        entries = []
        for o in objs:
            if o.get("schema"):
                WRITES.add(o["schema"])
            o.setdefault("tenant", self.tenant)
            vt = {"from": o[valid_from_field]} if valid_from_field and o.get(valid_from_field) else None
            entries.append((o, vt))
        if self._kbatch:
            self._kbatch(entries, asserted_at=asserted_at)
        else:
            for o, vt in entries:
                self._kwrite(o["id"], o, valid_time=vt, asserted_at=asserted_at)
        return len(objs)

    @property
    def file(self):
        """The filestore surface — `ctx.file.put(bytes|str) → id`, `ctx.file.get(id) → bytes`.
        Content-addressed (the id IS the sha2-256 digest): write-once + dedup, and possessing the
        digest is the read capability. Same code path host (local blob), broker (/v1/file), and
        `aos test` (in-memory). Using it derives the meta `files: [put|get]` declaration."""
        return _FileAPI(self)

    def fetch(self, url, method="GET", body=None, inject=None, headers=None):
        """Brokered external EGRESS — the ONE way a confined component reaches an outside host when a
        secret must stay hidden. The broker checks this run's declared egress + rate limit, injects the
        named secret + declared User-Agent/auth SERVER-SIDE, and audits the call; the component never sees
        the key. `inject` names the secret ("FIRMS_MAP_KEY" or {"name": ...}). Returns a FetchResult
        (.status/.ok/.body/.json()/.headers). Public/keyless egress can still use plain urllib directly."""
        if self._ffetch is None:
            raise Reject("ctx.fetch unavailable in this runtime (no brokered-egress capability)")
        inj = {"name": inject} if isinstance(inject, str) else (inject or None)
        host = url.split("://", 1)[-1].split("/", 1)[0].split("@")[-1].split(":")[0]
        if host:
            EGRESS.add(host)                       # observed egress host → derives meta.egress
        if inj and inj.get("name"):
            SECRETS.add(inj["name"])               # observed injected secret → derives meta.secrets
        r = self._ffetch(url, method, body, inj, headers) or {}
        return FetchResult(r.get("upstream_status"), r.get("body"), r.get("headers"))

    def ai(self, messages, max_tokens=900):
        if not self._ai:
            raise Reject("ai() unavailable in this runtime")
        return self._ai(messages, max_tokens)

    def call(self, engine_id, verb, payload):
        if not self._call:
            raise Reject("call() unavailable in this runtime")
        return self._call(engine_id, verb, payload)

    # ── host-only capabilities (in-process engines): cross-tenant reads, network/model gateways, install,
    #    federation. Confined drivers (broker/headless) do NOT get these — each raises a clear Reject. ──
    def _cap(self, name):
        c = self._caps.get(name)
        if c is None:
            raise Reject(f"ctx.{name} is a host capability (in-process engine only) — unavailable in this runtime")
        return c

    def facts_x(self, schema, tenant, where=None):
        """AGGREGATOR cross-tenant READ (ADR-0072): resolve ANOTHER tenant's rows of `schema` via THIS engine's
        own cross-tenant grant — the lane a program engine uses to read many agencies' data. Records a READ."""
        READS.add(schema)
        return Query(self._cap("kfind_x")(schema, tenant, where) or [], schema)

    def http(self, method, url, payload=None, token=None, headers=None):
        """Host-held HTTP to a platform service (network/kernel/model gateway). Engines call other services this
        way rather than hand-rolling urllib; use ctx.network_url / ctx.kernel_url to build the URL."""
        return self._cap("http")(method, url, payload, token, headers)

    @property
    def network_url(self):
        return self._caps.get("network_url")

    @property
    def kernel_url(self):
        return self._caps.get("kernel_url")

    def embed(self, texts, kind="query"):
        """Embed text via the host model gateway (RAG engines) — the gateway URL stays host-side."""
        return self._cap("embed")(texts, kind)

    def ensure_install(self, component_id):
        """Register a per-tenant install-as-grant (mints the scheduler's run-token) — e.g. a self-serve toggle."""
        return self._cap("ensure_install")(component_id)

    def federate(self, provider, workflow, title, payload, parent_case):
        """ADR-0097 W2: open a graph node's leg in a PROVIDER tenant under its own authority (host mints the token)."""
        return self._cap("federate")(provider, workflow, title, payload, parent_case)

    def remote_status(self, provider, remote_case):
        """Read a federated leg's status back (cross-tenant, host-held provider token)."""
        return self._cap("remote_status")(provider, remote_case)

    def can(self, capability):
        """True if this runtime grants a host-only capability (e.g. 'kfind_x', 'http', 'federate').
        Lets a component take the own-tenant / degraded path cleanly instead of catching Reject."""
        return capability in self._caps

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
    put/get callables (host local-blob / broker /v1/file / in-memory). Every call records into
    FILES so meta derives `files: [put|get]` (never hand-declared)."""
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


class FetchResult:
    """The result of ctx.fetch — the broker's {upstream_status, body, headers} in an ergonomic shape.
    body is the raw response text; .json() parses it; .ok is a 2xx check."""
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body or ""
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= (self.status or 0) < 300

    def json(self):
        return json.loads(self.body or "null")

    def __repr__(self):
        return f"<FetchResult {self.status} {len(self.body)}B>"


def _valid_period(vfrom, vto):
    """valid_from/valid_to shorthands → the kernel's {from, to?} valid_time window (None if neither given)."""
    if vfrom is None and vto is None:
        return None
    p = {}
    if vfrom is not None:
        p["from"] = vfrom
    if vto is not None:
        p["to"] = vto
    return p


def _host_kwrite(host_write):
    """Wrap the in-process host kwrite(oid, obj) so ctx.write can pass valid_time/asserted_at. Older host
    ctx predates the envelope — forward it when accepted, else fall back to the bare write (graceful)."""
    def kw(oid, obj, valid_time=None, asserted_at=None):
        if valid_time is not None or asserted_at is not None:
            try:
                return host_write(oid, obj, valid_time=valid_time, asserted_at=asserted_at)
            except TypeError:
                pass
        return host_write(oid, obj)
    return kw


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

    def kwrite(oid, obj, valid_time=None, asserted_at=None):
        body = {"scope": obj.get("schema") or _scope_from_id(oid), "id": oid, "object": obj}
        if valid_time is not None:                         # bitemporal ASSERTION window {from,to?}
            body["valid_time"] = valid_time
        if asserted_at is not None:                        # transaction time (default epoch server-side)
            body["asserted_at"] = asserted_at
        return _req("POST", "/v1/write", body)

    def kbatch(entries, asserted_at=None):
        items = []
        for o, vt in entries:                              # entries = [(object, valid_time|None), …]
            it = {"scope": o.get("schema") or _scope_from_id(o["id"]), "id": o["id"], "object": o}
            if vt is not None:
                it["valid_time"] = vt
            items.append(it)
        body = {"items": items}
        if asserted_at is not None:
            body["asserted_at"] = asserted_at
        return _req("POST", "/v1/write/batch", body)

    def kget(oid, scope=None):
        return (_req("POST", "/v1/read", {"scope": scope or _scope_from_id(oid), "id": oid}) or {}).get("object")

    def ai(messages, max_tokens=900):
        r = _req("POST", "/v1/complete", {"messages": messages, "max_tokens": max_tokens}) or {}
        return r.get("content", r)

    def fput(b):    # broker filestore PUT (PLAT-FILE-002): base64 over the JSON seam → content-addressed id
        return (_req("POST", "/v1/file/put", {"content_b64": base64.b64encode(b).decode()}) or {}).get("id")

    def fget(fid):  # broker filestore GET: the digest IS the read capability → raw bytes back
        return base64.b64decode((_req("POST", "/v1/file/get", {"id": fid}) or {}).get("content_b64") or "")

    def ffetch(url, method, body_, inject, headers):
        """Brokered EGRESS: the broker resolves this run's declared egress + rate limit, injects the named
        secret + declared User-Agent/auth SERVER-SIDE, and audits to prov.egress. The component never sees
        the key. Returns the raw {upstream_status, body, headers} dict."""
        payload = {"url": url, "method": (method or "GET").upper()}
        if body_ is not None:
            payload["body"] = body_
        if inject:
            payload["inject"] = inject          # {"name": "<secret>"} — broker injects; component never sees the value
        return _req("POST", "/v1/fetch", payload) or {}

    tenant = (_req("GET", "/v1/context") or {}).get("tenant") if body is None else None
    return Ctx(body=body, tenant=tenant, kfind=kfind, kwrite=kwrite, kget=kget, kbatch=kbatch,
               ai=ai, fput=fput, fget=fget, ffetch=ffetch)


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


# ── connector — read-side adapter over an external system of record (data-connectors S0) ─────────────
# A connector resolves a GOVERNED schema by reading from an external source LIVE, instead of a driver having
# copied the data in. It registers a per-schema reader the kernel resolver calls: `read(q) -> connector.page(...)`.
# Same confinement/broker/Vault machinery as a driver; the difference is DIRECTION (read, not write) and that the
# resolver invokes it in-band. The warm pooled serve loop is the runtime's job (S1); run()/run_local() here give a
# headless one-shot so a connector is authorable + testable now.
class ConnectorQuery:
    """The governed query the resolver sends. Source-agnostic; the reader maps it to the source's native call."""
    def __init__(self, d):
        d = d or {}
        self.schema = d.get("schema"); self.tenant = d.get("tenant")
        self.where = d.get("where") or []            # [{field, op, value}] AND-ed
        self.projection = d.get("projection")         # [field] or None = all
        self.sort = d.get("sort") or []               # [{field, dir}]
        self.limit = d.get("limit") or 100
        self.cursor = d.get("cursor")                 # opaque continuation
        self.as_of = d.get("as_of")                   # only honoured if capabilities.as_of
        self.raw = d
    def get(self, field, op="eq"):
        """Convenience: first `where` value for (field, op), else None — e.g. q.get('ward')."""
        for w in self.where:
            if w.get("field") == field and w.get("op", "eq") == op:
                return w.get("value")
        return None


class _Connector:
    COMPLETE = "complete"; PARTIAL = "partial"; UNAVAILABLE = "unavailable"

    def __init__(self):
        self._readers = {}        # schema -> fn(ConnectorQuery) -> page

    def read(self, schema):
        """@connector.read('water.complaint') — register the reader for a governed schema this connector serves."""
        def deco(fn):
            self._readers[schema] = fn
            return fn
        return deco

    def page(self, rows, cursor=None, completeness=None, observed_at=None, source_ref=None):
        """Build the governed page. `completeness` = {status, reason?, evaluated_rows?} (default complete).
        `observed_at` = when the source actually answered (freshness); defaults to now()."""
        comp = completeness if isinstance(completeness, dict) else {"status": completeness or self.COMPLETE}
        return {"rows": list(rows or []), "cursor": cursor, "observed_at": observed_at or now(),
                "completeness": comp, "source_ref": source_ref}

    def partial(self, rows, reason, evaluated_rows=None, cursor=None, **kw):
        """Shortcut for a partial page — the source could not fully honour the query (see capabilities)."""
        c = {"status": self.PARTIAL, "reason": reason}
        if evaluated_rows is not None:
            c["evaluated_rows"] = evaluated_rows
        return self.page(rows, cursor=cursor, completeness=c, **kw)

    def readers(self):
        return dict(self._readers)

    def _serve_one(self, q):
        qo = ConnectorQuery(q)
        fn = self._readers.get(qo.schema)
        if not fn:
            raise Reject(f"connector serves no reader for schema {qo.schema!r}")
        return fn(qo)

    def run(self):
        """Headless one-shot (S0): resolve ONE query from env AIRAWAT_QUERY (JSON) or stdin, print the page as JSON.
        The resolver-driven warm serve loop is wired in the runtime (S1)."""
        if not self._readers:
            raise Reject("no @connector.read registered")
        import sys as _sys
        cx = Ctx.from_broker()
        token = _current.set(cx)
        try:
            raw = os.environ.get("AIRAWAT_QUERY") or ("" if _sys.stdin.isatty() else _sys.stdin.read()) or "{}"
            pg = self._serve_one(json.loads(raw))
            print(json.dumps(pg))
            return pg
        finally:
            _current.reset(token)

    def run_local(self, query, store=None, tenant="you-dev", fetches=None):
        """`aos test` path: serve ONE query against an in-memory ctx (seed `fetches={url: body}` for the source)."""
        if not self._readers:
            raise Reject("no @connector.read registered")
        cx = Ctx.in_memory(store, tenant, fetches=fetches) if "fetches" in Ctx.in_memory.__code__.co_varnames \
            else Ctx.in_memory(store, tenant)
        token = _current.set(cx)
        try:
            return self._serve_one(query)
        finally:
            _current.reset(token)

    def registered(self):
        return bool(self._readers)

    def main(self):
        """Entry a connector's run.py calls: warm serve loop when AIRAWAT_CONNECTOR_MODE=serve (the launcher
        sets it for a `runtime.mode: warm` placement), else the headless one-shot. One authoring model, both runtimes."""
        import os as _os
        if (_os.environ.get("AIRAWAT_CONNECTOR_MODE") or "").lower() == "serve":
            return self.serve()
        return self.run()

    def serve(self, port=None, host="0.0.0.0"):
        """Warm pooled runtime (S2b): a long-lived HTTP server the kernel's resolver calls per query.
        POST /read  {schema, tenant, where[], projection?, sort[], limit, cursor?, as_of?}  → the page JSON
        GET  /healthz → {status, schemas:[...]}
        The connector holds ONE broker ctx for its lifetime (minted from RUN_TOKEN); each request is served
        under it. This is the address the runtime-manager registers in platform.connector_runtime, so the
        kernel discovers it by record, not by a hardcoded endpoint. Blocks forever (the container stays up)."""
        if not self._readers:
            raise Reject("no @connector.read registered")
        import os as _os, json as _json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        port = int(port or _os.environ.get("CONNECTOR_PORT", "9900"))
        try:                                             # process-wide broker ctx for the serving lifetime;
            _current.set(Ctx.from_broker())              # a ctx-free connector (fixed/offline source) needs none
        except Exception as _e:
            print(f"[connector] no broker ctx at startup ({_e}); readers that call ctx.* will fail per-request")
        schemas = list(self._readers.keys())
        _serve_one = self._serve_one

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = _json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path.rstrip("/") == "/healthz":
                    return self._send(200, {"status": "ok", "schemas": schemas})
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                if self.path.rstrip("/") != "/read":
                    return self._send(404, {"error": "not found"})
                try:
                    q = _json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except Exception:
                    return self._send(400, {"error": "bad json"})
                try:
                    return self._send(200, _serve_one(q))
                except Reject as r:
                    return self._send(422, {"error": str(r)})
                except Exception as e:
                    return self._send(502, {"error": f"connector read failed: {str(e)[:200]}"})

        print(f"[connector] warm serve on :{port}  schemas={schemas}")
        ThreadingHTTPServer((host, port), _H).serve_forever()


connector = _Connector()

from . import ingest   # aos.ingest — generic tabular-ingest primitive (additive; imported last so ctx/Reject exist)
