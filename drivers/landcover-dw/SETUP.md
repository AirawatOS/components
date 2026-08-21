# landcover-dw — setup & wiring (GEE Dynamic World, certified container)

Land cover from **Google Dynamic World** via Earth Engine, written as per-cell `cell.builtup` /
`cell.greencover` fractions. Runs in the **shared `earth-observation` runtime container** (ADR-0030,
`components/runtimes/earth-observation`) — the driver ships no image of its own; `earthengine-api` lives in
the runtime, mounted with the driver code. **No synthetic fallback**: without credentials it fails loudly.

You have three values: `GEE_PROJECT`, `GEE_SERVICE_ACCOUNT` (the SA email), and `GEE_SERVICE_ACCOUNT_KEY`
(the SA key JSON). Only the **key** is a secret → Vault. Project + email are non-secret run config.

## 1. Build → stamp → certify the earth-observation runtime image (once, shared by all EO drivers)

The launcher runs ONLY the certified image, so after building you must stamp its digest + re-certify:
```
docker build -t airawatos/runtime-earth-observation:0.1.0 components/runtimes/earth-observation
docker image inspect --format '{{.Id}}' airawatos/runtime-earth-observation:0.1.0   # → put as image_digest: in meta.yaml
# then certify (steward): POST /v1/certify {subject_id: ds:airawat:runtime/earth-observation, subject_kind: runtime, meta_yaml, files}
```
(See `components/runtimes/README.md` → "Build → stamp → certify".)

## 2. Store the SA key in Vault (secrets service — tenant-admin)

The key lives at `secret/<tenant>/ds:airawat:driver/landcover-dw/GEE_SERVICE_ACCOUNT_KEY`; the tenant
supplies it at install and rotates it later. It is NEVER in code, the registry, or the component.

```
# as a tenant-admin (Bearer = a tenant-admin token for <tenant>, e.g. bmc)
curl -s -X POST http://localhost:8102/set \
  -H "Authorization: Bearer $TENANT_ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"tenant\":\"bmc\",\"component\":\"ds:airawat:driver/landcover-dw\",
       \"name\":\"GEE_SERVICE_ACCOUNT_KEY\",\"value\":$(cat sa-key.json | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')}"
```

## 3. Run it (per tenant)

The launcher (a) materialises the key from Vault via the secrets service `/materialize` (internal-token
gated — only the launcher may call it), and (b) `docker run`s the container with the confined run token +
broker url + the GEE env. The raw key is injected into the **container's** env only (the container's
declared egress is the trust boundary); it never touches host Python.

```
KEY=$(curl -s -X POST http://localhost:8102/materialize -H "X-Internal-Token: $SECRETS_INTERNAL" \
  -H 'Content-Type: application/json' \
  -d '{"tenant":"bmc","component":"ds:airawat:driver/landcover-dw","name":"GEE_SERVICE_ACCOUNT_KEY"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["value"])')

docker run --rm --network airawat-stack_default \
  -v "$PWD/components/drivers/landcover-dw:/drivers:ro" \
  -e AIRAWAT_API=http://kernel:8080 \
  -e RUN_TOKEN=dev-run-landcover-dw@bmc \
  -e AOI_ID=ds:bmc:space/aoi/greater-mumbai--pilot -e TENANT=bmc \
  -e GEE_PROJECT="$GEE_PROJECT" -e GEE_SERVICE_ACCOUNT="$GEE_SERVICE_ACCOUNT" \
  -e GEE_SERVICE_ACCOUNT_KEY="$KEY" \
  airawatos/runtime-earth-observation:0.1.0  python /drivers/run.py
```
(The launcher does this for you — placing on `RUNTIME_NETWORK` and deriving `kernel:8080`. On Docker Desktop
without the stack network, drop `--network` and use `--add-host host.docker.internal:host-gateway` +
`AIRAWAT_API=http://host.docker.internal:8080`.)

For a quick **local dev test** (no Vault), export the three GEE_* vars and run `python run.py` directly —
but note the host confinement shim (L1) would block the ee client's direct GEE calls, which is exactly why
this driver is a **container** (L2): its egress policy allows the broker **and** the declared GEE hosts.

## 4. Swap it into the City Baseline pipeline

`landcover-dw` REPLACES the OSM `landcover` acquire + the two `cell-indexer` fraction steps (built + green)
— it writes `cell.builtup` / `cell.greencover` directly and scales to a metro (GEE reduces server-side).
In `components/pipelines/city-baseline/run.py`, swap those three `MEMBERS` entries for a single
`("acquire+index land cover (Dynamic World)", "drivers/landcover-dw", "dev-run-landcover-dw", {})` — as a
container step (the launcher `docker run`s it) rather than a host subprocess. Keep OSM `landcover` available
for tenants without GEE credentials (real-or-fail: pick the source you have creds for).

## What's still needed to run end-to-end

- **Restart the secrets service** so `/materialize` is live (it's a host process; needs its Vault env).
- **Container-launch step in the pipeline/executor** — today the pipeline runs members as host subprocesses;
  a container member needs the launcher to `docker run` with the materialised env (small executor addition).
- Your GEE credentials loaded per step 2. Then it verifies end-to-end; until then it fails loudly by design.
