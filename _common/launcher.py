#!/usr/bin/env python3
"""Reference LAUNCHER — implements the `platform.runtime_invoke` interface (ADR-0030/0058).

Given a component directory + a confined run token + env, it EITHER runs the component IN-PROCESS (no
`runtime.profile` declared — the current reference default) OR PLACES it in its runtime CONTAINER:

  1. resolve  `runtime.profile` → the runtime component `ds:airawat:runtime/<profile>`
  2. enforce  the runtime's CERTIFICATE at POINT OF USE (ADR-0051) — refuse an uncertified/unverified runtime
  3. inject   declared secrets, materialised from Vault via the secrets service (never logged)
  4. run      `docker run` the certified runtime image with the code mounted READ-ONLY at /drivers, broker as
              the only registry door, egress = broker + the component's declared hosts, under the run token

Returns (returncode, combined_output). The ADR-0050 execution engine will hoist this to a platform service;
here it is a library the pipeline/orchestrator calls so profiled components run in their containers today.

Env it reads: NETWORK_URL · SECRETS_URL · SECRETS_INTERNAL · CONTAINER_BROKER_URL · AIRAWAT_API · TIMEOUT
"""
import json, os, subprocess, sys, urllib.request

NETWORK_URL = os.environ.get("NETWORK_URL", "http://localhost:8090")
SECRETS_URL = os.environ.get("SECRETS_URL", "http://localhost:8102")
SECRETS_INTERNAL = os.environ.get("SECRETS_INTERNAL", "")
# PLACEMENT NETWORK. In production the runtime container joins the SAME docker network as the kernel and
# reaches the broker by its service alias (`kernel:8080`) — service DNS, no host gateway. Set RUNTIME_NETWORK
# to that network (e.g. airawat-stack_default). Unset ⇒ Docker-Desktop dev fallback: default bridge + the
# host gateway (host.docker.internal). An explicit CONTAINER_BROKER_URL always wins.
RUNTIME_NETWORK = os.environ.get("RUNTIME_NETWORK", "")
KERNEL_ALIAS = os.environ.get("KERNEL_ALIAS", "kernel")      # the broker's in-network service name
CONTAINER_BROKER_URL = os.environ.get("CONTAINER_BROKER_URL") or (
    f"http://{KERNEL_ALIAS}:8080" if RUNTIME_NETWORK else "http://host.docker.internal:8080")
TIMEOUT = int(os.environ.get("LAUNCH_TIMEOUT", "1200"))
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


def _resolve_cert(subject_id):
    """Fetch the CA certificate for a subject. /v1/certificates/resolve returns the cert record DIRECTLY
    (id/status/subject/conformance/signature); a miss returns {detail: "..."}."""
    try:
        r = _post(NETWORK_URL + "/v1/certificates/resolve", {"subject_id": subject_id})
        if not isinstance(r, dict) or r.get("detail"):
            return None
        return r.get("certificate") or (r if (r.get("subject") or r.get("status")) else None)
    except Exception:
        return None


def _runtime_image(profile):
    """The image tag from the runtime component's own meta (components/runtimes/<profile>/meta.yaml)."""
    d = os.path.join(COMPONENTS, "runtimes", profile)
    return _read_meta(d).get("image") if os.path.isfile(os.path.join(d, "meta.yaml")) else None


def _materialize(tenant, component, name):
    """Raw secret from Vault via the secrets service (internal-gated). Returns None if unset."""
    try:
        r = _post(SECRETS_URL + "/materialize", {"tenant": tenant, "component": component, "name": name},
                  {"X-Internal-Token": SECRETS_INTERNAL})
        return r.get("value")
    except Exception:
        return None


def place(component_dir, run_token, env=None, *, label=""):
    """Run a component. Returns (returncode, output). runtime.profile ⇒ certified container; else in-process."""
    env = {k: str(v) for k, v in (env or {}).items()}
    meta = _read_meta(component_dir)
    entry = meta.get("entrypoint", "run.py")
    profile = (meta.get("runtime") or {}).get("profile")

    if not profile:                                      # IN-PROCESS (reference default) — host subprocess
        e = dict(os.environ, RUN_TOKEN=run_token,
                 AIRAWAT_API=os.environ.get("AIRAWAT_API", "http://localhost:8080"), **env)
        p = subprocess.run([sys.executable, os.path.join(component_dir, entry)],
                           env=e, capture_output=True, text=True, timeout=TIMEOUT)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    # CONTAINER PLACEMENT ------------------------------------------------------------------------------
    runtime_id = f"ds:airawat:runtime/{profile}"
    cert = _resolve_cert(runtime_id)
    if not cert or not (cert.get("conformance") or {}).get("verified"):
        return 87, (f"REFUSED (ADR-0051 point-of-use): runtime {runtime_id} is not certified — "
                    f"cannot place {label or component_dir}. Certify the runtime first.")
    image = _runtime_image(profile)
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
    cmd += ["-v", f"{os.path.abspath(component_dir)}:/drivers:ro",
            "-e", f"AIRAWAT_API={CONTAINER_BROKER_URL}", "-e", f"RUN_TOKEN={run_token}"]
    for k, v in {**env, **secret_env}.items():           # config + materialised secrets → container env only
        cmd += ["-e", f"{k}={v}"]
    cmd += [image, "python", f"/drivers/{entry}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        return 89, "REFUSED: docker not available to place a container runtime"
    return p.returncode, (p.stdout or "") + (p.stderr or "")
