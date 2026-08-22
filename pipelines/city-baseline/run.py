#!/usr/bin/env python3
"""city-baseline: the onboarding pipeline (ADR-0060). ONE AOI → a City Digital Baseline.

The onboarding spine, end to end:
  request    file/advance the city.onboarding_request (the commissioner's 'set up my city')
  provision  write a city.workspace (maturity L0 Requested)
  acquire    run the L0→L1 drivers: land cover (OSM), heat (Open-Meteo), terrain (elevation)
  index      run cell-indexer: land cover → cell.builtup / cell.greencover ; LST → cell.heat
  assess     run coverage-qa → city.coverage (per-layer coverage, source, confidence, authority)
  advance    workspace maturity L0 → L1 (Baseline)
  notify     write a platform.inbox_item to the commissioner: 'Your City Baseline is ready'

Each member runs as its own confined component (its own run-token + declared caps); this orchestrator only
writes the workspace / request / inbox. In production the ADR-0050 execution engine runs the members under
minted tokens — here the pipeline invokes them directly (drivers are host subprocesses in the reference build).

Env: AOI_ID · TENANT · WORKSPACE · COMMISSIONER (role:// or user://) · PRIORITIES (comma list)
"""
import json, os, sys, subprocess, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/kanpur-core")
TENANT = os.environ.get("TENANT", "kmc")
WSID = os.environ.get("WORKSPACE", f"ds:{TENANT}:city/workspace/{AOI_ID.rsplit('/', 1)[-1]}")
REQID = f"ds:{TENANT}:city/onboarding_request/{AOI_ID.rsplit('/', 1)[-1]}"
COMMISSIONER = os.environ.get("COMMISSIONER", f"role://{TENANT}/urban.commissioner")
PRIORITIES = [p.strip() for p in os.environ.get("PRIORITIES", "growth,heat").split(",") if p.strip()]
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
COMPONENTS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CI = "engines/cell-indexer"

# (label, component dir, run-token, extra env) — the ordered baseline pipeline
MEMBERS = [
    # Land cover from Google Dynamic World (GEE) — writes cell.builtup + cell.greencover FRACTIONS directly,
    # server-side reduced (scales to a metro). Runs in the certified earth-observation runtime via the manager.
    # Replaces the OSM-landcover acquire + the two cell-indexer fraction steps. (No GEE creds ⇒ fails loudly.)
    ("land cover → built + green (Dynamic World)", "drivers/landcover-dw", "dev-run-landcover-dw", {}),
    ("acquire heat (Open-Meteo)",  "drivers/heat", "dev-run-heat", {"MODE": "openmeteo"}),
    ("index → cell.heat",          CI, "dev-run-cell", {"INPUT_SCHEMA": "environment.lst_observation",
                                    "OUTPUT_SCHEMA": "cell.heat", "METHOD": "idw", "VALUE_FIELD": "lst_value"}),
    # terrain from Copernicus GLO-30 DEM via GEE (server-side, scales to a metro; runs in the EO runtime).
    # Replaces the Open-Meteo point-fetch terrain driver. (No GEE creds for terrain-dem ⇒ fails loudly.)
    ("terrain (Copernicus DEM)",   "drivers/terrain-dem", "dev-run-terrain-dem", {}),
    ("assess coverage & trust",    "engines/coverage-qa", "dev-run-coverage", {}),
]


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def write(scope, oid, obj):
    call("/v1/write", {"scope": scope, "id": oid, "object": obj, "valid_time": {"from": NOW}, "asserted_at": NOW})


def request(status, name, res):
    write("city.onboarding_request", REQID, {"schema": "city.onboarding_request", "id": REQID,
        "ulb_code": TENANT, "ulb_name": name, "requested_by": COMMISSIONER, "boundary_ref": AOI_ID,
        "priorities": PRIORITIES, "status": status, "requested_at": NOW})


def workspace(level, status, name, res):
    write("city.workspace", WSID, {"schema": "city.workspace", "id": WSID, "tenant": TENANT,
        "ulb_code": TENANT, "ulb_name": name, "boundary_ref": AOI_ID, "h3_resolution": res,
        "maturity_level": level, "status": status, "created_at": NOW})


RUNTIME_MANAGER_URL = os.environ.get("RUNTIME_MANAGER_URL")   # placement authority (point 1); else in-process


def _place(reldir, run_token, env, label):
    """Ask the RUNTIME MANAGER to run a member — the pipeline holds NO docker/secret power, it only REQUESTS a
    placement (platform.runtime_invoke). Falls back to an in-process launcher only when no manager is
    configured (local dev)."""
    if RUNTIME_MANAGER_URL:
        body = {"component": reldir, "run_token": run_token, "env": env, "label": label}
        req = urllib.request.Request(RUNTIME_MANAGER_URL + "/v1/invoke", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "X-Internal-Token": os.environ.get("RUNTIME_MANAGER_INTERNAL", "")}, method="POST")
        r = json.loads(urllib.request.urlopen(req, timeout=1300).read())
        return r.get("exit_code", 1), r.get("logs", "")
    sys.path.insert(0, os.path.join(COMPONENTS, "_common"))
    import launcher
    return launcher.place(os.path.join(COMPONENTS, reldir), run_token, env, label=label)


def run_member(label, reldir, token, env):
    # Onboarding tokens are tenant-generic: the run's tenant rides in the token name as "<base>@<tenant>"
    # so ONE base drives the baseline for ANY city (the kernel clones the base spec per tenant). See ADR-0060.
    member_token = f"{token}@{TENANT}"
    # non-secret run config. WRITE_TENANT is what members stamp into output ids (ds:<tenant>:…) — must match
    # the run's tenant. The manager decides placement from the member's meta: runtime.profile ⇒ its certified
    # container (cert-checked, image-locked, secrets materialised); else an in-process host subprocess.
    e = dict(AOI_ID=AOI_ID, TENANT=TENANT, WRITE_TENANT=TENANT, **env)
    try:
        rc, out = _place(reldir, member_token, e, label)
    except Exception as ex:
        print(f"  [FAIL] {label}: {ex}"); return False
    tail = ((out or "").strip().splitlines() or ["(no output)"])[-1][:120]
    print(f"  [{'ok  ' if rc == 0 else 'FAIL'}] {label}: {tail}")
    if rc != 0:
        print(f"         {(out or '').strip()[-300:]}")
    return rc == 0


def main():
    aoi = call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object") or {}
    name = aoi.get("name", AOI_ID.rsplit("/", 1)[-1]); res = int(aoi.get("h3_res", 9))
    print(f"City Baseline · AOI '{name}' ({AOI_ID}) · workspace {WSID}\n")

    request("building", name, res)                      # L0: the commissioner's request, now building
    workspace(0, "building", name, res)                 # L0 Requested → provisioning

    print("running baseline pipeline:")
    ok = sum(run_member(*m) for m in MEMBERS)

    workspace(1, "ready", name, res)                    # L1 Baseline
    request("ready", name, res)
    iid = f"ds:{TENANT}:platform/inbox_item/baseline-ready-{WSID.rsplit('/', 1)[-1]}"
    write("platform.inbox_item", iid, {"schema": "platform.inbox_item", "id": iid, "actor": COMMISSIONER,
        "title": f"Your City Baseline for {name} is ready",
        "body": (f"The City Digital Baseline for {name} has been generated ({ok}/{len(MEMBERS)} steps). "
                 "Open City Scan to explore built-up, green cover, heat and terrain over the ward, with a "
                 "coverage & trust report showing what is authoritative vs AI-derived."),
        "ref": WSID, "kind": "notification", "status": "unread", "created_at": NOW})

    print(f"\n✓ baseline ready — {ok}/{len(MEMBERS)} steps · workspace → L1 Baseline · notified {COMMISSIONER}")
    print(f"RESULT: {json.dumps({'workspace': WSID, 'maturity': 1, 'steps_ok': ok, 'notified': COMMISSIONER})}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
