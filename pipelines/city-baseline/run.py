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

EXECUTION (slice-1c-iv step 3): this pipeline does NOT place its members and holds NO service credential. It
runs in two phases under the SCHEDULER (which owns execution and delegates placement to the runtime manager):
  PHASE=pre  (default) — file the request + provision the workspace (L0), then DECLARE the member plan on stdout
                         (`AIROS_PIPELINE_PLAN <json>`). The scheduler runs each member as ITS OWN installed
                         component — its own per-tenant install run-token + declared caps — via the runtime manager.
  PHASE=post           — the scheduler passes STEPS_OK / STEPS_TOTAL / STEP_RESULTS; advance L0 → L1 + notify.
The pipeline's only I/O is the broker (its run token). A member that is not installed for the tenant simply fails
its step (reported), never a widening.

Env: AOI_ID · TENANT · WORKSPACE · COMMISSIONER (role:// or user://) · PRIORITIES (comma list) · PHASE
"""
import json, os, sys, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
# The pipeline builds exactly ONE city, named by its caller (the onboarding bridge supplies both from the
# commissioner's request). NO default — a defaulted tenant/AOI is how one city's baseline lands on another's.
TENANT = os.environ.get("TENANT") or ""
AOI_ID = os.environ.get("AOI_ID") or ""
if not TENANT or not AOI_ID:
    raise SystemExit("FATAL: city-baseline requires TENANT and AOI_ID (which city to build) — there is no default.")
_at = AOI_ID.split(":")[1] if AOI_ID.count(":") >= 2 else ""
if _at and _at != TENANT:                                        # the AOI must belong to the write tenant, never another city's
    raise SystemExit(f"FATAL: AOI_ID {AOI_ID} belongs to tenant '{_at}', not TENANT '{TENANT}' — refusing to build.")
WSID = os.environ.get("WORKSPACE", f"ds:{TENANT}:city/workspace/{AOI_ID.rsplit('/', 1)[-1]}")
REQID = f"ds:{TENANT}:city/onboarding_request/{AOI_ID.rsplit('/', 1)[-1]}"
COMMISSIONER = os.environ.get("COMMISSIONER", f"role://{TENANT}/urban.commissioner")
PRIORITIES = [p.strip() for p in os.environ.get("PRIORITIES", "growth,heat").split(",") if p.strip()]
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
PHASE = (os.environ.get("PHASE") or "pre").strip().lower()

# (label, member "<kind>/<name>" — resolved by the scheduler under THIS pipeline's steward, extra env) — the ordered
# baseline. Members run under their OWN per-tenant install tokens (install-as-grant), not tokens named here.
MEMBERS = [
    # Land cover from Google Dynamic World (GEE) — writes cell.builtup + cell.greencover FRACTIONS directly,
    # server-side reduced (scales to a metro). Runs in the certified earth-observation runtime via the manager.
    # Replaces the OSM-landcover acquire + the two cell-indexer fraction steps. (No GEE creds ⇒ fails loudly.)
    ("land cover → built + green (Dynamic World)", "driver/landcover-dw", {}),
    ("acquire heat (Open-Meteo)",  "driver/heat", {"MODE": "openmeteo"}),
    ("index → cell.heat",          "engine/cell-indexer", {"INPUT_SCHEMA": "environment.lst_observation",
                                    "OUTPUT_SCHEMA": "cell.heat", "METHOD": "idw", "VALUE_FIELD": "lst_value"}),
    # terrain from Copernicus GLO-30 DEM via GEE (server-side, scales to a metro; runs in the EO runtime).
    # Replaces the Open-Meteo point-fetch terrain driver. (No GEE creds for terrain-dem ⇒ fails loudly.)
    ("terrain (Copernicus DEM)",   "driver/terrain-dem", {}),
    ("assess coverage & trust",    "engine/coverage-qa", {}),
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


def plan():
    """The member plan the SCHEDULER executes (it validates + resolves each member under this pipeline's steward,
    and runs it under that member's own per-tenant install token via the runtime manager)."""
    return [{"label": label, "member": member, "env": env} for label, member, env in MEMBERS]


def main():
    aoi = call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object") or {}
    name = aoi.get("name", AOI_ID.rsplit("/", 1)[-1]); res = int(aoi.get("h3_res", 9))
    if PHASE == "pre":
        print(f"City Baseline · AOI '{name}' ({AOI_ID}) · workspace {WSID}\n")
        request("building", name, res)                  # L0: the commissioner's request, now building
        workspace(0, "building", name, res)             # L0 Requested → provisioning
        print("AIROS_PIPELINE_PLAN " + json.dumps(plan()))   # the scheduler runs these members (not this process)
        return 0
    if PHASE != "post":
        raise SystemExit(f"FATAL: unknown PHASE '{PHASE}' (pre | post)")
    ok = int(os.environ.get("STEPS_OK") or 0); total = int(os.environ.get("STEPS_TOTAL") or len(MEMBERS))
    for r in json.loads(os.environ.get("STEP_RESULTS") or "[]"):
        print(f"  [{'ok  ' if r.get('status') == 'ok' else 'FAIL'}] {r.get('label') or r.get('member')}: {r.get('status')}")
    workspace(1, "ready", name, res)                    # L1 Baseline
    request("ready", name, res)
    iid = f"ds:{TENANT}:platform/inbox_item/baseline-ready-{WSID.rsplit('/', 1)[-1]}"
    write("platform.inbox_item", iid, {"schema": "platform.inbox_item", "id": iid, "actor": COMMISSIONER,
        "title": f"Your City Baseline for {name} is ready",
        "body": (f"The City Digital Baseline for {name} has been generated ({ok}/{total} steps). "
                 "Open City Scan to explore built-up, green cover, heat and terrain over the ward, with a "
                 "coverage & trust report showing what is authoritative vs AI-derived."),
        "ref": WSID, "kind": "notification", "status": "unread", "created_at": NOW})
    print(f"\n✓ baseline ready — {ok}/{total} steps · workspace → L1 Baseline · notified {COMMISSIONER}")
    print(f"RESULT: {json.dumps({'workspace': WSID, 'maturity': 1, 'steps_ok': ok, 'notified': COMMISSIONER})}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
