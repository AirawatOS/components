# Runtime profiles — shared execution containers (ADR-0030)

A **runtime profile** is a shared, per-profile container image that bundles a coherent library set. A
component (driver / engine) does **not** ship its own image — it declares which profile it needs:

```yaml
runtime:
  profile: earth-observation      # the PLACEMENT KEY. omit ⇒ standard in-process (host subprocess).
```

…and the launcher runs it **inside that profile's container**, with the component code mounted read-only at
`/drivers`. The component's only I/O is the broker (`AIRAWAT_API`) plus its own **declared egress**. One image
serves every component on the profile — deps are built once, not per component. (Ported from AirOS
`specs/11-runtime-spec.md`, which had `geospatial` / `earth-observation` / `modeling` runners.)

## Why

- Heavy/unusual deps (earthengine-api, GDAL, torch) stay **out of the host Python and the coordinator** —
  the reason the DW driver couldn't just be a host subprocess.
- A component stays tiny (just `run.py` + `meta.yaml`); the platform owns the runtime.
- Isolation is real: an L2 container whose egress is broker + declared hosts (vs the L1 host-shim, which
  allows *only* the broker and would block a client like `ee`).

## The profiles (start set — extend as needed)

| profile | analytics tier | provides (common libs) | used by |
|---|---|---|---|
| **geospatial** | diagnostic | h3, shapely, geopandas, pyproj, rasterio, networkx | cell-indexer, accessibility, connectivity |
| **earth-observation** | descriptive | earthengine-api, rasterio, numpy/scipy, pyproj, earthaccess, h3 | landcover-dw (GEE Dynamic World), satellite/raster |
| **modeling** | predictive | torch, scikit-learn, xgboost, numpy/scipy | air-quality forecast, PM2.5 fusion, risk models |

**Two axes, deliberately.** `profile` = the *dependency* set (what libs the code needs). `analytics_tier`
= the *purpose* (descriptive → observe raw facts · diagnostic → describe/relate place · predictive →
forecast · prescriptive → recommend). The tier is how the catalog/marketplace groups engines for a user;
the profile is how the platform places them. A prescriptive/optimization runtime (e.g. OR-Tools) is the
natural next addition.

## Build → stamp → certify (required before a runtime may host anything)

The launcher runs **only the certified image** (`LAUNCHER_REQUIRE_CERTIFIED_IMAGE=1`, default). It checks two
bindings at point of use (ADR-0051): the on-disk runtime **source** equals its certified digest, AND the
local **image**'s content id equals the `image_digest` the certified `meta.yaml` declares. So publishing a
runtime is a three-step loop:

```
# 1. build the image
docker build -t airawatos/runtime-geospatial:0.1.0 components/runtimes/geospatial
# 2. stamp its content id into meta.yaml as `image_digest:`
docker image inspect --format '{{.Id}}' airawatos/runtime-geospatial:0.1.0     # → sha256:…  (put in meta.yaml)
# 3. certify (steward) — evidence_digest now covers the stamped meta
POST /v1/certify {subject_id: ds:airawat:runtime/geospatial, subject_kind: runtime, meta_yaml, files}
```

Rebuild the image ⇒ its id changes ⇒ re-stamp + re-certify. A stale or swapped image is refused, not run.
(Set `LAUNCHER_REQUIRE_CERTIFIED_IMAGE=0` only for local dev where a cert-verified runtime is enough.)

## Launcher contract (what runs a profiled component)

The launcher (`components/_common/launcher.py`, implements `platform.runtime_invoke`) resolves the profile →
certified runtime, **enforces its certificate at point-of-use** (ADR-0051), materialises declared secrets
from Vault, then places the container on the **kernel's network** so it reaches the broker by service DNS:

```
# PRODUCTION — RUNTIME_NETWORK set (e.g. airawat-stack_default): join the kernel's network, broker = kernel:8080
docker run --rm --network airawat-stack_default \
  -v <component-dir>:/drivers:ro \
  -e AIRAWAT_API=http://kernel:8080 \
  -e RUN_TOKEN=<dev-run-…@tenant> -e AOI_ID=… -e TENANT=… \
  [ -e <SECRET>=<materialized-from-Vault> … ] \
  airawatos/runtime-<profile>:<ver>  python /drivers/run.py

# DEV FALLBACK — RUNTIME_NETWORK unset (Docker Desktop): default bridge + host gateway
#   --add-host host.docker.internal:host-gateway   -e AIRAWAT_API=http://host.docker.internal:8080
```

Placement env (the app-host injects these into the pipeline): `RUNTIME_NETWORK` (network to join),
`KERNEL_ALIAS` (broker service name, default `kernel`), `CONTAINER_BROKER_URL` (explicit override),
`NETWORK_URL`/`SECRETS_URL`/`SECRETS_INTERNAL` (the launcher's own cert-resolve + secret-materialise).

`city-baseline` members without a profile still run in-process (host subprocess); a `runtime.profile` member
is placed in its certified container.

**The placement authority (point 1).** Docker + secret power lives in ONE trusted service, the **runtime
manager** (`platform-spec/reference/runtime_manager.py`, `POST /v1/invoke`, internal-gated) — it imports the
launcher. An orchestrator (a pipeline, the ADR-0050 engine) sets `RUNTIME_MANAGER_URL` and REQUESTS a
placement; it never touches the Docker socket itself. So a component can't start a container, only ask the
manager to, and the manager runs it under the governed policy (certified runtime + certified image + declared
egress + run token). Unset ⇒ the pipeline falls back to an in-process launcher (local dev).

**Profiles generalise across kinds.** `runtime.profile` is not driver-specific: an **engine** picks
`geospatial`/`modeling`, a **pipeline** an `orchestration` runtime, an **ai-skill** an `agent` runtime, a
**provider** a `gateway` runtime — each a certified image the manager places the component into. Add profiles
as new kinds need them; the manager + interface are unchanged.
