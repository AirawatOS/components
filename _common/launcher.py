#!/usr/bin/env python3
"""Reference LAUNCHER — implements the `platform.runtime_invoke` interface (ADR-0030/0058).

Given a component directory + a confined run token + env, it EITHER runs the component IN-PROCESS (no
`runtime.profile` declared — the current reference default) OR PLACES it in its runtime CONTAINER:

  1. resolve  `runtime.profile` → the runtime component `ds:airawatdeveloper:runtime/<profile>`
  2. enforce  the runtime's CERTIFICATE at POINT OF USE (ADR-0051) — refuse an uncertified/unverified runtime
  3. inject   declared secrets, materialised from Vault via the secrets service (never logged)
  4. run      `docker run` the certified runtime image with the code mounted READ-ONLY at /drivers, broker as
              the only registry door, egress = broker + the component's declared hosts, under the run token

Returns (returncode, combined_output). The ADR-0050 execution engine will hoist this to a platform service;
here it is a library the pipeline/orchestrator calls so profiled components run in their containers today.

Env it reads: NETWORK_URL · SECRETS_URL · SECRETS_INTERNAL · CONTAINER_BROKER_URL · AIRAWAT_API · TIMEOUT
"""
import hashlib, json, os, subprocess, sys, urllib.request, urllib.parse

NETWORK_URL = os.environ.get("NETWORK_URL", "http://localhost:8090")
# UNIVERSAL DELIVERY (Runtime-vs-Component split): a component's SOURCE is fetched from the content-addressed
# filestore and digest-verified at point of use, instead of arriving via the rt-components bind-mount. The heavy
# shared runtime IMAGE still comes from the container registry (see ENFORCE_IMAGE below) — filestore = code,
# registry = runtime.
FILESTORE_URL = os.environ.get("FILESTORE_URL", "http://localhost:8095")
COMPONENT_CACHE = os.environ.get("COMPONENT_CACHE", "/tmp/airawatos-components")   # writable rehydrate dir
SECRETS_URL = os.environ.get("SECRETS_URL", "http://localhost:8102")
# slice-1c-iii: SECRETS_INTERNAL retired — _materialize mints an audience-bound svc:secrets token (svc_token).
# PLACEMENT NETWORK. In production the runtime container joins the SAME docker network as the kernel and
# reaches the broker by its service alias (`kernel:8080`) — service DNS, no host gateway. Set RUNTIME_NETWORK
# to that network (e.g. airawat-stack_default). Unset ⇒ Docker-Desktop dev fallback: default bridge + the
# host gateway (host.docker.internal). An explicit CONTAINER_BROKER_URL always wins.
RUNTIME_NETWORK = os.environ.get("RUNTIME_NETWORK", "")
KERNEL_ALIAS = os.environ.get("KERNEL_ALIAS", "kernel")      # the broker's in-network service name
CONTAINER_BROKER_URL = os.environ.get("CONTAINER_BROKER_URL") or (
    f"http://{KERNEL_ALIAS}:8080" if RUNTIME_NETWORK else "http://host.docker.internal:8080")
TIMEOUT = int(os.environ.get("LAUNCH_TIMEOUT", "1200"))
# LOCK: the launcher runs ONLY an image the certificate attests. On ⇒ verify the on-disk runtime source
# equals its certified digest AND the local image equals the certified image_digest. Off (dev) ⇒ cert-verified
# runtime is enough. Default ON — this is the point-of-use image binding (ADR-0051) for the artifact that runs.
ENFORCE_IMAGE = os.environ.get("LAUNCHER_REQUIRE_CERTIFIED_IMAGE", "1") != "0"
HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENTS = os.path.dirname(HERE)                       # …/components (runtimes/ is a sibling)


def _read_meta(component_dir):
    """Minimal meta.yaml reader for the fields the launcher needs — avoids a yaml dependency in the host
    python. Handles top-level scalars, the nested `runtime:` block, and inline `[a, b]` lists."""
    meta, runtime, in_rt = {}, {}, False
    for raw in open(os.path.join(component_dir, "meta.yaml")):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        k, _, v = line.strip().partition(":")
        if indent == 0:
            in_rt = line.strip().startswith("runtime:")
            if v.strip():
                meta[k.strip()] = v.split("#", 1)[0].strip()
        elif in_rt:
            runtime[k.strip()] = v.split("#", 1)[0].strip()
    meta["runtime"] = runtime
    return meta


def _list(v):
    if not v:
        return []
    return [x.strip() for x in str(v).strip().lstrip("[").rstrip("]").split(",") if x.strip()]


def _post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=15).read() or b"{}")


def _resolve_cert(subject_id, network_url=None):
    """Fetch the CA certificate for a subject. /v1/certificates/resolve returns the cert record DIRECTLY
    (id/status/subject/conformance/signature); a miss returns {detail: "..."}. For a FEDERATED component the
    cert lives on the OWNING participant's network — pass its base url; trust is still the digest vs this cert
    (a hardening step verifies the remote cert's signature against the participant's directory key)."""
    try:
        r = _post((network_url or NETWORK_URL) + "/v1/certificates/resolve", {"subject_id": subject_id})
        if not isinstance(r, dict) or r.get("detail"):
            return None
        return r.get("certificate") or (r if (r.get("subject") or r.get("status")) else None)
    except Exception:
        return None


def _dir_digest(d):
    """Reproduce conformance.aggregate_digest / conform.file_manifest EXACTLY: sha2-256 of the canonical
    {relpath: sha256(contents)} map over the WHOLE tree (recursive, excluding __pycache__/.git and *.pyc/*.log),
    so it equals the cert's evidence_digest for EVERY component — flat drivers AND engines/apps with subdirs.
    (A top-level-only hash matched flat drivers but REFUSED any component with a subdirectory — the blocker to
    retiring the rt-components bind-mount and delivering all components from the filestore.)"""
    files = {}
    for root, dirs, names in os.walk(d):
        dirs[:] = [x for x in dirs if x not in ("__pycache__", ".git")]
        for n in names:
            if n.endswith((".pyc", ".log")):
                continue
            p = os.path.join(root, n)
            files[os.path.relpath(p, d)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    canon = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return "sha2-256:" + hashlib.sha256(canon).hexdigest()


def _runtime_source_file_id(cid, network_url=None):
    """ADR-0095 R2: the `source_file_id` of a runtime's certified meta/source tar in the filestore, from the
    network Store listing (set at publish). Uses /v1/store/resolve (resolve-by-id) because /v1/store BROWSE hides
    kind=runtime from non-operators. None ⇒ not filestore-delivered (baked/local)."""
    try:
        req = urllib.request.Request((network_url or NETWORK_URL) + "/v1/store/resolve",
            data=json.dumps({"component_id": cid}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        r = json.loads(urllib.request.urlopen(req, timeout=15).read())
        l = r.get("listing") or r
        return l.get("source_file_id") or None
    except Exception:
        return None


def _fetch_and_verify(cid, source_file_id, cert, filestore_url=None):
    """UNIVERSAL DELIVERY point-of-use gate: pull a component's SOURCE tar.gz from the content-addressed
    filestore and VERIFY its dir-digest == the certificate's evidence_digest BEFORE it may run. Returns the
    extracted dir on success, or None (REFUSE) on any miss/mismatch — untrusted bytes never run un-verified,
    so distribution over HTTPS carries no privilege (trust is the digest + the cert, not the transport).
    CROSS-PARTICIPANT: `filestore_url` points at the OWNING participant's filestore for a FEDERATED component;
    the content-address + cert-digest gate makes a remote pull exactly as safe as a local one."""
    import io, tarfile, shutil
    if not source_file_id or not cert:
        return None
    name = (cid.rsplit("/", 1)[-1] if cid else str(source_file_id).replace(":", "-"))
    dest = os.path.join(COMPONENT_CACHE, name)
    try:
        # ADR-0124 slice-1c-ii: the filestore gate ENFORCES — attach a svc:filestore token minted as the
        # `launcher` principal (the scheduler host's Vault token reads its password). Fail-closed: no token →
        # header omitted → the gate refuses (a missing-credential misconfig must not fall open).
        _fs_hdr = {}
        try:
            import svc_token as _svc_fs
            _t = _svc_fs.token(principal="launcher", tenant=os.environ.get("SERVICE_TENANT", "localdev-operator"), audience="svc:filestore")
            if _t:
                _fs_hdr["Authorization"] = "Bearer " + _t
        except Exception:
            pass
        _req = urllib.request.Request(
            (filestore_url or FILESTORE_URL) + "/files/" + urllib.parse.quote(str(source_file_id), safe=":"), headers=_fs_hdr)
        blob = urllib.request.urlopen(_req, timeout=60).read()
        if os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)       # never merge over stale bytes
        os.makedirs(dest, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            tf.extractall(dest)
    except Exception as e:
        print(f"[launcher] source fetch failed for {cid}: {str(e)[:120]}")
        return None
    want = cert.get("evidence_digest")
    got = _dir_digest(dest)
    if want and got != want:
        print(f"[launcher] REFUSE {cid}: source digest {got[:22]}… ≠ certified {want[:22]}…")
        return None
    return dest


def _local_image_id(image):
    """The content id (sha256) of the LOCAL image for `image` — None if it isn't present."""
    try:
        r = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _runtime_manifest_meta(cid, profile, cert):
    """Resolve a runtime's certified meta dir (on-disk under COMPONENTS/runtimes/<profile>, else materialised
    from the filestore + digest-verified), returning (runtime_dir, rmeta) — or (None, {}) on a miss."""
    runtime_dir = os.path.join(COMPONENTS, "runtimes", profile)
    if not os.path.isfile(os.path.join(runtime_dir, "meta.yaml")):
        sfid = _runtime_source_file_id(cid)
        got = _fetch_and_verify(cid, sfid, cert) if sfid else None
        if not got:
            return None, {}
        runtime_dir = got
    return runtime_dir, _read_meta(runtime_dir)


def attest_runtimes(node=None, network_url=None):
    """ADR-0095 R3 / ADR-0086 (advisory): report to the Network which certified runtime IMAGES this node
    holds (host-integrity). For each kind=runtime listing: resolve+verify its certified meta, compare the
    LOCAL docker image .Id to the certified image_digest (and the on-disk source to the cert digest), and
    POST /v1/runtime/attest. Best-effort; never raises (called on the runtime-manager boot)."""
    import socket
    node = node or os.environ.get("NODE_ID") or socket.gethostname()
    net = network_url or NETWORK_URL
    # step 5: audience-bound svc:network token minted as the `launcher` principal (no shared literal; fail-closed)
    try:
        import svc_token as _svc_att
        tok = _svc_att.token(principal="launcher", tenant=os.environ.get("SERVICE_TENANT", "localdev-operator"),
                             audience="svc:network")
    except Exception:
        tok = None
    if not tok:
        print("[attest] no svc:network token for the launcher principal — skipping attestation (fail-closed)")
        return
    try:
        r = urllib.request.urlopen(net + "/v1/store?kind=runtime", timeout=15)
        listings = (json.loads(r.read() or b"{}") or {}).get("listings", [])
    except Exception as e:
        print(f"[attest] could not list runtimes from {net}: {e}")
        return
    for l in listings:
        cid = l.get("component_id") or ""
        profile = cid.split("/")[-1]
        try:
            cert = _resolve_cert(cid, net)
            runtime_dir, rmeta = _runtime_manifest_meta(cid, profile, cert)
            image = (rmeta or {}).get("image", "")
            want = (rmeta or {}).get("image_digest", "")
            have = _local_image_id(image) if image else None
            dir_ok = bool(cert and cert.get("evidence_digest") and runtime_dir
                          and _dir_digest(runtime_dir) == cert["evidence_digest"])
            match = bool(have and want and have == want and dir_ok)
            _post(net + "/v1/runtime/attest",
                  {"runtime_id": cid, "version": l.get("version"), "node": node, "image": image,
                   "local_image_id": have or "", "certified_image_digest": want, "match": match},
                  {"Authorization": "Bearer " + tok})
            print(f"[attest] {profile}: match={match} image={'present' if have else 'absent'} src_ok={dir_ok}")
        except Exception as e:
            print(f"[attest] {profile}: {e}")


def _materialize(tenant, component, name):
    """Raw secret from Vault via the secrets service. ADR-0124 slice-1c-iii: authenticate with an audience-bound
    svc:secrets token minted as the `launcher` principal (fail-closed: no token → gate refuses). Returns None if unset."""
    try:
        hdr = {}
        try:
            import svc_token as _svc_sec
            _t = _svc_sec.token(principal="launcher", tenant=os.environ.get("SERVICE_TENANT", "localdev-operator"), audience="svc:secrets")
            if _t:
                hdr["Authorization"] = "Bearer " + _t
        except Exception:
            pass
        r = _post(SECRETS_URL + "/materialize", {"tenant": tenant, "component": component, "name": name}, hdr)
        return r.get("value")
    except Exception:
        return None


def place(component_dir, run_token, env=None, *, label="", cid=None, source_file_id=None, source_origin=None):
    """Run a component. Returns (returncode, output). runtime.profile ⇒ certified container; else in-process.

    UNIVERSAL DELIVERY (Runtime-vs-Component split): if the source isn't on-disk and the authorized caller
    supplied a `source_file_id`, pull+digest-verify it from the marketplace filestore into a writable cache and
    run from there — no rt-components bind-mount needed. Source already present on-disk (baked / dev) is used
    as-is. The caller (install/scheduler, which holds auth) resolves the component_asset → source_file_id."""
    env = {k: str(v) for k, v in (env or {}).items()}
    if source_file_id and not os.path.isfile(os.path.join(component_dir, "meta.yaml")):
        # CROSS-PARTICIPANT: source_origin={filestore,network} points at the OWNING participant's endpoints for a
        # FEDERATED component; absent ⇒ this participant's own filestore/network (the common case).
        _org = source_origin or {}
        got = _fetch_and_verify(cid, source_file_id,
                                _resolve_cert(cid, _org.get("network")) if cid else None,
                                filestore_url=_org.get("filestore"))
        if not got:
            return 87, (f"REFUSED (universal delivery): could not fetch + digest-verify source for "
                        f"{label or cid or component_dir} from the filestore.")
        component_dir = got
    meta = _read_meta(component_dir)
    entry = meta.get("entrypoint", "run.py")
    profile = (meta.get("runtime") or {}).get("profile")

    if not profile:                                      # IN-PROCESS (reference default) — host subprocess
        # CLEAN env (slice-1c-iv step 2 / DEBT-B51 partial): the child gets ONLY its run token, the broker URL, the
        # SDK path and its declared non-secret config — NEVER the placement host's environment. The runtime manager
        # holds a Vault token (to mint the launcher's svc:secrets/svc:filestore tokens); inheriting it would hand a
        # component that power. Mirrors host.py's clean env.
        e = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/tmp", "RUN_TOKEN": run_token,
             "AIRAWAT_API": os.environ.get("AIRAWAT_API", "http://localhost:8080"), **env}
        # Put the `aos` SDK on the child's import path so a component that `import aos` works in the reference
        # in-process runtime (no baked runtime image). The SDK dir is the FIRST of these that actually holds an
        # `aos/` package — HERE (repo layout), the in-container install, or an explicit override — because this
        # launcher may itself run from a materialised copy (rt-components) that doesn't sit beside the SDK.
        # Runtime wiring only, not ambient authority (egress/reads stay broker-gated).
        _sdk = next((d for d in (os.environ.get("AOS_SDK_PATH"), HERE, "/app/components/_common")
                     if d and os.path.isdir(os.path.join(d, "aos"))), HERE)
        e["PYTHONPATH"] = _sdk + ((os.pathsep + e["PYTHONPATH"]) if e.get("PYTHONPATH") else "")
        p = subprocess.run([sys.executable, os.path.join(component_dir, entry)],
                           env=e, capture_output=True, text=True, timeout=TIMEOUT)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    # CONTAINER PLACEMENT ------------------------------------------------------------------------------
    runtime_id = f"ds:airawatdeveloper:runtime/{profile}"
    cert = _resolve_cert(runtime_id)
    if not cert or not (cert.get("conformance") or {}).get("verified"):
        return 87, (f"REFUSED (ADR-0051 point-of-use): runtime {runtime_id} is not certified — "
                    f"cannot place {label or component_dir}. Certify the runtime first.")
    runtime_dir = os.path.join(COMPONENTS, "runtimes", profile)
    if not os.path.isfile(os.path.join(runtime_dir, "meta.yaml")):
        # ADR-0095 R2: an UNBAKED runtime — materialise its certified META/source from the filestore + digest-verify
        # (the cert was resolved + verified just above). The IMAGE stays in the registry; only the runtime source
        # rides the filestore, so the runtime-manager image can ship services-only too. Falls through to the same
        # ENFORCE_IMAGE gate below (which re-checks _dir_digest == cert AND local image .Id == certified image_digest).
        _sfid = _runtime_source_file_id(runtime_id)
        _got = _fetch_and_verify(runtime_id, _sfid, cert) if _sfid else None
        if not _got:
            return 87, (f"REFUSED: runtime profile '{profile}' is not on disk and could not be materialised from "
                        f"the filestore (no source_file_id or digest mismatch) — publish its source, then retry.")
        runtime_dir = _got
    rmeta = _read_meta(runtime_dir)
    image = rmeta.get("image")
    if ENFORCE_IMAGE:
        # LOCK to the certified image. (1) on-disk runtime SOURCE must equal what was certified — so the
        # Dockerfile / image tag / declared digest cannot be swapped after certification.
        if cert.get("evidence_digest") and _dir_digest(runtime_dir) != cert["evidence_digest"]:
            return 87, (f"REFUSED: runtime '{profile}' on-disk source ≠ its certified digest — "
                        f"re-certify the runtime after any change before it may host a component.")
        # (2) the IMAGE that will actually run must equal the digest the (now-trusted) certified meta declares.
        want, have = rmeta.get("image_digest"), _local_image_id(image)
        if not want:
            return 87, (f"REFUSED: runtime '{profile}' has no certified image_digest — build the image, "
                        f"stamp its digest into meta.yaml, and re-certify before it can run.")
        if not have:
            return 87, f"REFUSED: certified image {image} is not present locally — build/pull it first."
        if have != want:
            return 87, (f"REFUSED: image {image} ({have[:26]}…) ≠ certified digest ({want[:26]}…) — "
                        f"only the certified image may run. Rebuild from certified source or re-certify.")
    if not image:
        return 87, f"REFUSED: no image for runtime profile '{profile}' (components/runtimes/{profile}/meta.yaml)"

    secret_env = {}
    for name in _list(meta.get("secrets")):
        val = _materialize(env.get("TENANT", "airawat"), meta.get("component_id", ""), name)
        if val is None:
            return 88, (f"REFUSED (424): secret '{name}' not configured for {meta.get('component_id')} "
                        f"in tenant '{env.get('TENANT')}' — set it via the secrets service before running.")
        secret_env[name] = val

    cmd = ["docker", "run", "--rm"]
    if RUNTIME_NETWORK:                                   # prod: place on the kernel's network → service DNS
        cmd += ["--network", RUNTIME_NETWORK]
    else:                                                # dev (Docker Desktop): reach the host loopback broker
        cmd += ["--add-host", "host.docker.internal:host-gateway"]
    # HOST-PATH INVARIANT: this bind is resolved by the HOST docker daemon (docker-in-docker), so
    # `component_dir` must name a path that exists on the HOST at this same absolute path. That holds for BOTH
    # source channels: the rt-components mount AND the universal-delivery COMPONENT_CACHE are each bind-mounted
    # into the runtime-manager at their identical host path — so a filestore-rehydrated (docker-profile)
    # component mounts into its runtime exactly like a bind-mounted one. Keep COMPONENT_CACHE host-consistent.
    cmd += ["-v", f"{os.path.abspath(component_dir)}:/drivers:ro"]
    _common = os.path.abspath(os.path.join(component_dir, "..", "..", "_common"))  # shared helpers (ADR-0063 ids, etc.)
    if os.path.isdir(_common):
        cmd += ["-v", f"{_common}:/_common:ro"]   # drivers insert <up3>/_common on sys.path → /_common in-container

    # ALL env (broker + config + MATERIALISED SECRETS) goes via a private --env-file, NEVER `-e VALUE`: an
    # argv value leaks into `docker inspect`, `ps`, and any subprocess exception/log (e.g. TimeoutExpired). The
    # file is 0600, in the manager's own FS, and unlinked in `finally`. (Values must be single-line — API keys
    # / the GEE service-account JSON already are.)
    import tempfile as _tf
    allenv = {"AIRAWAT_API": CONTAINER_BROKER_URL, "RUN_TOKEN": run_token, **env, **secret_env}
    ef = _tf.NamedTemporaryFile("w", prefix="airawat-run-", suffix=".env", delete=False)
    try:
        for k, v in allenv.items():
            ef.write(f"{k}={v}\n")
        ef.close()
        os.chmod(ef.name, 0o600)
        cmd += ["--env-file", ef.name, image, "python", f"/drivers/{entry}"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        except FileNotFoundError:
            return 89, "REFUSED: docker not available to place a container runtime"
        except subprocess.TimeoutExpired:
            return 124, f"REFUSED: run exceeded LAUNCH_TIMEOUT ({TIMEOUT}s) — bound the work or raise the cap"
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    finally:
        try: os.unlink(ef.name)
        except OSError: pass


# ── WARM placement (data-connectors S2b): a long-lived pooled server, not a one-shot batch run ──────────────
# The read path can't `docker run --rm` a fresh container per query. A `runtime.mode: warm` component is launched
# ONCE as a detached, NAMED container on the kernel's network; the kernel reaches it by that name (discovered via
# the platform.connector_runtime record the kernel writes — ADR option B) and pools the connection. place_warm is
# idempotent: an already-running, healthy container is returned as-is.
CONNECTOR_PORT = int(os.environ.get("CONNECTOR_PORT", "9900"))
# Dev fallback image for a warm connector when its `connector` runtime isn't certified yet. A warm connector is
# pure-stdlib (broker SDK bind-mounted), so a slim python runs it — but then it is NOT image-locked/egress-confined,
# so the record is stamped confined:false. Prod certifies the `connector` runtime (image-lock + declared egress) =
# the S2c hardening follow-on. Never silently unconfined: the flag rides the governed runtime record.
CONNECTOR_DEV_IMAGE = os.environ.get("CONNECTOR_DEV_IMAGE", "python:3.12-slim")


def _container_state(name):
    """(running: bool, healthy_hint: str) for a docker container by name — '' if absent."""
    try:
        r = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", name],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return (False, "")
        return (r.stdout.strip() == "true", "")
    except Exception:
        return (False, "")


def _healthz(base_url, tries=20, delay=0.5):
    """Poll GET <base_url>/healthz until ok. Returns the health JSON or None."""
    import time
    for _ in range(tries):
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + "/healthz", timeout=4) as f:
                return json.loads(f.read() or b"{}")
        except Exception:
            time.sleep(delay)
    return None


def place_warm(component_dir, run_token, env=None, *, name, label="", cid=None,
               source_file_id=None, source_origin=None):
    """Launch (or reuse) a component as a long-lived WARM server; return a dict the kernel records:
      {ok, container, base_url, address, image, profile, confined, launched_at, ttl_s, health}
    on_disk source is used as-is; universal delivery rehydrates from the filestore when source_file_id is given.
    `name` is the container name the kernel discovers (e.g. connector-rest-connector)."""
    env = {k: str(v) for k, v in (env or {}).items()}
    # Reuse an already-healthy container (idempotent) — reach it by service name on the placement network.
    base_url = f"http://{name}:{CONNECTOR_PORT}"
    running, _ = _container_state(name)
    if running:
        h = _healthz(base_url, tries=4, delay=0.4)
        if h is not None:
            return {"ok": True, "reused": True, "container": name, "base_url": base_url,
                    "address": base_url + "/read", "health": h, "profile": None, "confined": None,
                    "launched_at": None, "ttl_s": int(os.environ.get("CONNECTOR_TTL_S", "90"))}
        # running but unhealthy → recycle it so we re-create cleanly
        try: subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30)
        except Exception: pass

    if source_file_id and not os.path.isfile(os.path.join(component_dir, "meta.yaml")):
        _org = source_origin or {}
        got = _fetch_and_verify(cid, source_file_id,
                                _resolve_cert(cid, _org.get("network")) if cid else None,
                                filestore_url=_org.get("filestore"))
        if not got:
            return {"ok": False, "error": f"universal delivery: could not fetch+verify source for {label or cid}"}
        component_dir = got
    meta = _read_meta(component_dir)
    entry = meta.get("entrypoint", "run.py")
    profile = (meta.get("runtime") or {}).get("profile")

    # Resolve the image: the certified runtime profile when present+certified (CONFINED), else the dev base image.
    image, confined = CONNECTOR_DEV_IMAGE, False
    if profile:
        runtime_id = f"ds:airawatdeveloper:runtime/{profile}"
        cert = _resolve_cert(runtime_id)
        runtime_dir = os.path.join(COMPONENTS, "runtimes", profile)
        if cert and (cert.get("conformance") or {}).get("verified") and os.path.isfile(os.path.join(runtime_dir, "meta.yaml")):
            rmeta = _read_meta(runtime_dir)
            rimg = rmeta.get("image")
            if ENFORCE_IMAGE:
                want, have = rmeta.get("image_digest"), (_local_image_id(rimg) if rimg else None)
                locked = (not cert.get("evidence_digest") or _dir_digest(runtime_dir) == cert["evidence_digest"]) \
                         and want and have and have == want
                if locked:
                    image, confined = rimg, True
                # else: certified runtime present but image mismatch → fall back to dev image (confined:false, loud)
            elif rimg:
                image, confined = rimg, True
    if not confined:
        print(f"[launcher] WARM {label or name}: running UNCONFINED on {image} "
              f"(runtime profile '{profile}' not certified/locked) — record stamped confined:false (S2c hardening pending)")

    # Materialise declared secrets (same as cold place) — passed via --env-file, never argv.
    secret_env = {}
    for sname in _list(meta.get("secrets")):
        val = _materialize(env.get("TENANT", "airawat"), meta.get("component_id", ""), sname)
        if val is None:
            return {"ok": False, "error": f"secret '{sname}' not configured for {meta.get('component_id')} "
                                          f"in tenant '{env.get('TENANT')}'"}
        secret_env[sname] = val

    cmd = ["docker", "run", "-d", "--name", name, "--restart", "unless-stopped"]
    if RUNTIME_NETWORK:
        cmd += ["--network", RUNTIME_NETWORK]
    else:
        cmd += ["--add-host", "host.docker.internal:host-gateway"]
    cmd += ["-v", f"{os.path.abspath(component_dir)}:/drivers:ro"]
    _common = os.path.abspath(os.path.join(component_dir, "..", "..", "_common"))
    if os.path.isdir(_common):
        cmd += ["-v", f"{_common}:/_common:ro"]   # ids.py + shared helpers (does NOT need an aos/ here)
    # The `aos` SDK for a warm connector comes from an ISOLATED dir bound at /connector_sdk — so the dev base
    # image gets the SDK WITHOUT live-swapping the shared in-process _common/aos. Prefer an explicit
    # CONNECTOR_SDK_DIR, else <COMPONENTS>/_connector_sdk, else the baked /app/components/_common (certified image).
    pythonpath = "/_common"
    _sdk_host = os.environ.get("CONNECTOR_SDK_DIR") or os.path.join(COMPONENTS, "_connector_sdk")
    if os.path.isdir(os.path.join(_sdk_host, "aos")):
        cmd += ["-v", f"{os.path.abspath(_sdk_host)}:/connector_sdk:ro"]
        pythonpath = "/connector_sdk:/_common"
    import tempfile as _tf
    allenv = {"AIRAWAT_API": CONTAINER_BROKER_URL, "RUN_TOKEN": run_token,
              "AIRAWAT_CONNECTOR_MODE": "serve", "CONNECTOR_PORT": str(CONNECTOR_PORT),
              "PYTHONPATH": pythonpath, **env, **secret_env}
    ef = _tf.NamedTemporaryFile("w", prefix="airawat-warm-", suffix=".env", delete=False)
    try:
        for k, v in allenv.items():
            ef.write(f"{k}={v}\n")
        ef.close(); os.chmod(ef.name, 0o600)
        cmd += ["--env-file", ef.name, image, "python", f"/drivers/{entry}"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            return {"ok": False, "error": "docker not available to place a warm container"}
        if p.returncode != 0:
            return {"ok": False, "error": f"docker run failed: {(p.stderr or p.stdout or '')[:300]}"}
    finally:
        try: os.unlink(ef.name)
        except OSError: pass

    h = _healthz(base_url)
    if h is None:
        logs = ""
        try:
            lr = subprocess.run(["docker", "logs", "--tail", "40", name], capture_output=True, text=True, timeout=15)
            logs = (lr.stdout or "") + (lr.stderr or "")
        except Exception:
            pass
        return {"ok": False, "container": name, "image": image, "error": "warm container did not become healthy",
                "logs": logs[-800:]}
    import time as _t
    return {"ok": True, "container": name, "base_url": base_url, "address": base_url + "/read",
            "image": image, "profile": profile, "confined": confined, "health": h,
            "launched_at": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
            "ttl_s": int(os.environ.get("CONNECTOR_TTL_S", "90"))}
