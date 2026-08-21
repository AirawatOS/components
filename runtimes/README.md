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

## Build

```
docker build -t airawatos/runtime-earth-observation:0.1.0 components/runtimes/earth-observation
docker build -t airawatos/runtime-geospatial:0.1.0        components/runtimes/geospatial
docker build -t airawatos/runtime-modeling:0.1.0          components/runtimes/modeling
```

## Launcher contract (what runs a profiled component)

```
docker run --rm --add-host host.docker.internal:host-gateway \
  -v <component-dir>:/drivers:ro \
  -e AIRAWAT_API=http://host.docker.internal:8080 \
  -e RUN_TOKEN=<dev-run-…@tenant> -e AOI_ID=… -e TENANT=… \
  [ -e <SECRET>=<materialized-from-Vault> … ] \
  airawatos/runtime-<profile>:<ver>  python /drivers/run.py
```

Today `city-baseline` runs members as host subprocesses (in-process, no profile). Wiring the launcher to
place a `runtime.profile` component into its container is the remaining executor step
(`_run_city_baseline` in the app-host, or the ADR-0050 execution engine).
