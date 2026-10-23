# Build "Water Intelligence" yourself — from the Console

A hands-on guide for a developer **new to AirawatOS** to build the Water Intelligence solution end‑to‑end using the in‑platform **Console**. By the end you'll have a geocoded complaint dataset, two analysis engines, and a live operations dashboard — all **configurable** so the same code serves any water utility.

**Audience:** you can write Python and read HTML/JS. You do **not** need prior AirawatOS knowledge — this guide teaches the platform concepts as it goes.
**Time:** ~1–2 hours end to end. **Difficulty:** beginner‑friendly if you follow in order.

---

## Before you begin

1. **Get access.** You need a login on an AirawatOS instance with a **development (sandbox) tenant** — this guide assumes `airawatdeveloper`. If you don't have one, ask your platform admin to add you and grant the `studio.product-manager` (or `platform.tenant-admin`) role.
2. **A *tenant*** is an isolated workspace (an agency/org). Your data, components and config live inside your tenant; other tenants can't see them. A **sandbox/development tenant** relaxes some rules so you can build + test freely before publishing.
3. **Open the Console.** Sign in at your instance (e.g. `https://airos.airawat.org`), and from the desktop launch the **Console** app (or go to `/run?app=console`). You'll see a prompt like `you@airawatdeveloper $`. Everything below is typed here.
4. **Sanity check.** Type `help` to see the command list, and `whoami`/`components` to confirm you're signed in to your tenant. If `help` works, you're ready.

> Tip: the Console accepts a **short tail** for component names — `run water-complaints` resolves `ds:…:driver/water-complaints`. Full `ds:…` ids always work too.

---

## Concepts in 2 minutes (read this once)

AirawatOS is a **governed data platform**. You don't hand‑edit servers; you write small **components** that read and write **governed facts**.

- **Governed fact / "data":** a JSON record stored in the central **registry**, validated against a **schema** (a published JSON‑Schema). Facts are tenant‑scoped and attributed to whoever wrote them. Example schema: `water.complaint`.
- **Schema (noun):** the contract for a kind of record (`<domain>.<subject>`, e.g. `water.hotspot`). Governed by the **AirawatNetwork** (the standards body).
- **Three component kinds:**
  - **driver** — brings data *in* (ingest a CSV, call an API) or *out*. Writes facts.
  - **engine** — pure compute *over* governed facts (reads facts → writes derived facts). No external I/O.
  - **app** — a browser UI that *reads* governed facts (and calls governed actions) under the signed‑in user.
- **The broker / confinement:** drivers and engines run **confined** (a clean‑room sandbox, `confinement_min: L1`). Their *only* I/O is the SDK's `ctx` object, which talks to the registry through a broker — so a component can only touch the schemas it **declares**. This is why metas list `reads:`/`writes:` + `scopes:` (the declared read/write grants the broker enforces).
- **H3:** a hexagonal geospatial grid. We geocode each complaint to an **H3 res‑9 cell** (~0.1 km²) so we can aggregate "hot cells."
- **The `aos` SDK:** the thin Python library every driver/engine uses — `from aos import driver, ingest, ctx`. Apps use the browser `AOS` helper from `/shared/aos.js`. (Mini‑reference at the end.)

**Golden rule (no‑synthetic‑data):** read back what you write and report the live count; on empty input, write nothing and say so. Never fabricate.

---

## What you're building (the target)

```
complaints.csv ──[water-complaints]──▶ water.complaint (geocoded, H3)
                                            │
                 ┌──────────────────────────┼───────────────────────────┐
                 ▼                          ▼                            ▼
          [water-hotspots]           [water-signals]              (read by)
          water.hotspot        water.signal + water.overview   [water-dashboard]
```
Plus a **`water-setup`** driver that seeds the **vocabulary + configuration as data** (so nothing is hardcoded), and a **`water-docs`** driver that ships these guides.

| Component | Kind | Reads | Writes |
|---|---|---|---|
| `water-setup` | driver | — | water.category, water.record_kind, water.config |
| `water-complaints` | driver | water.category | water.complaint |
| `water-hotspots` | engine | water.complaint, water.record_kind, water.config | water.hotspot |
| `water-signals` | engine | water.complaint, water.category, water.record_kind, water.config | water.signal, water.overview |
| `water-dashboard` | app | water.overview, water.hotspot, water.signal, water.config, water.category | — |

---

## 1. Create the project

```
project new water-intelligence
```
✅ **You should see:** `✓ created project water-intelligence` and the prompt `cwd → /projects/water-intelligence`. A **project** groups your components; anything you scaffold while `cd`'d into it becomes a member and shows under `ls`. (Returning later: `cd /projects/water-intelligence`.)

---

## 2. Govern the schemas (and how sandbox vs approval actually works)

**Read this — it's the #1 thing newcomers get wrong.** There are two independent facts:

- **In your sandbox tenant you can write to a schema that isn't governed yet.** The registry only *validates* a record when a governed definition exists; an unknown schema is simply stored unvalidated. So you can build and run immediately — you are **not** blocked waiting for approval.
- **`schema propose` files a governance *request*.** It submits your schema to the AirawatNetwork; a steward approves it in **Network Admin → Schemas**. Approval is what makes the schema *official* — **and it is mandatory before you publish the solution to the marketplace** (§10). It is *not* required just to build/run in your sandbox.

So the practical flow: propose your schemas now (so they're in the approval pipeline), keep building regardless, and make sure they're approved before you publish.

```
schema propose water.category
schema propose water.record_kind
schema propose water.config
schema propose water.complaint
schema propose water.hotspot
schema propose water.signal
schema propose water.overview
```
`schema propose` opens an editor pre‑filled with a skeleton. Replace it with the definition below (id must be `<domain>.<subject>`; keep them **permissive** — do *not* set `additionalProperties:false` — so drivers can add fields). Paste each, save.

<details><summary><b>The 7 schema definitions (copy each into the editor)</b></summary>

`water.category` — the issue taxonomy:
```json
{ "type":"object", "required":["code","label"], "properties":{
  "code":{"type":"string"}, "label":{"type":"string"},
  "domain":{"type":"string"}, "keywords":{"type":"array","items":{"type":"string"}},
  "order":{"type":"integer"} } }
```
`water.record_kind` — contact‑type vocabulary:
```json
{ "type":"object", "required":["code","label"], "properties":{
  "code":{"type":"string"}, "label":{"type":"string"}, "is_complaint":{"type":"boolean"} } }
```
`water.config` — the Solution Configuration (one per tenant):
```json
{ "type":"object", "required":[], "properties":{
  "agency_name":{"type":"string"}, "dashboard_title":{"type":"string"}, "subtitle":{"type":"string"},
  "is_live":{"type":"boolean"}, "snapshot_caveat":{"type":"string"}, "zone_label":{"type":"string"},
  "attributions":{"type":"object"}, "severity":{"type":"object"}, "windows":{"type":"object"} } }
```
`water.complaint` — one geocoded complaint:
```json
{ "type":"object", "required":["registered_at"], "properties":{
  "tracking_no":{"type":"string"}, "category":{"type":"string"}, "raw_category":{"type":"string"},
  "subcategory":{"type":"string"}, "record_kind":{"type":"string"}, "status":{"type":"string"},
  "priority":{"type":"string"}, "source_channel":{"type":"string"}, "registered_at":{"type":"string"},
  "closed_at":{"type":"string"}, "reopens":{"type":"integer"}, "ward":{"type":"string"}, "zone":{"type":"string"},
  "pincode":{"type":"string"}, "centroid":{"type":"array"}, "h3":{"type":"string"},
  "geocode_method":{"type":"string"}, "geocode_confidence":{"type":"number"} } }
```
`water.hotspot` — a hot H3 cell:
```json
{ "type":"object", "required":["h3","observed_at"], "properties":{
  "h3":{"type":"string"}, "ward":{"type":"string"}, "zone":{"type":"string"}, "count":{"type":"integer"},
  "dominant":{"type":"string"}, "category":{"type":"string"}, "severity":{"type":"string"},
  "window":{"type":"object"}, "observed_at":{"type":"string"}, "rate_per_10k":{"type":"number"},
  "wow_change":{"type":"number"} } }
```
`water.signal` — an operational signal:
```json
{ "type":"object", "required":["signal_type","observed_at"], "properties":{
  "signal_type":{"type":"string"}, "scope":{"type":"string"}, "key":{"type":"string"},
  "metric":{"type":"number"}, "metric_name":{"type":"string"}, "summary":{"type":"string"},
  "severity":{"type":"string"}, "observed_at":{"type":"string"}, "window":{"type":"object"},
  "ward":{"type":"string"}, "h3":{"type":"string"}, "category":{"type":"string"},
  "reliable":{"type":"boolean"} } }
```
`water.overview` — one compact projection the dashboard reads:
```json
{ "type":"object", "required":["observed_at"], "properties":{
  "observed_at":{"type":"string"}, "window":{"type":"object"}, "totals":{"type":"object"},
  "by_category":{"type":"array"}, "by_raw_category":{"type":"array"}, "by_zone":{"type":"array"},
  "by_channel":{"type":"array"}, "by_domain":{"type":"array"}, "by_status":{"type":"array"},
  "trend_daily":{"type":"array"} } }
```
</details>

✅ **You should see:** `✓ filed schema request <id> — pending`. (Pending is fine — keep going.)

---

## 3. Build the ingest driver — `water-complaints`

```
new driver water-complaints
```
✅ **You should see:** `✓ new driver water-complaints` and it added to the project. Now open its two files and paste from the **Appendix**:
```
edit water-complaints/meta.yaml      # paste the meta below
edit water-complaints/run.py         # paste Appendix → "water-complaints"
```
`meta.yaml`:
```yaml
component_id: ds:airawatdeveloper:driver/water-complaints
kind: driver
version: 0.1.0
provider: ds:airawatdeveloper:developer
display_name: Water Complaints — Ingest + Geocode
entrypoint: run.py
confinement_min: L1
reads: [water.category]
writes: [water.complaint]
scopes:
  - { verb: read,  scope: water.category }
  - { verb: write, scope: water.complaint }
config:
  - { name: COMPLAINTS_FILE_ID, kind: value }
  - { name: GAZETTEER_FILE_ID,  kind: value }
  - { name: RECORD_KIND_FIELD,  kind: value, default: Category }
```
**What this driver does (so you can adapt it):** it reads your complaint CSV, **auto‑detects** columns from the header (each overridable via an env var like `CATEGORY_FIELD`), normalizes the free‑text category to a canonical `code` using the governed `water.category` keywords, preserves the original label in `raw_category`, geocodes **complaints only** to an H3 cell via a gazetteer, and never stores PII (it reads `Address` only to pull a PIN, then drops it).

> **Why `reads:` *and* `scopes:`?** `reads:`/`writes:` are the human‑readable summary; `scopes:` is the machine list the broker enforces at runtime. Keep them in sync.

---

## 4. Build the engines

```
new engine water-hotspots
edit water-hotspots/meta.yaml        # see Appendix meta
edit water-hotspots/run.py           # paste Appendix → "water-hotspots"

new engine water-signals
edit water-signals/meta.yaml
edit water-signals/run.py            # paste Appendix → "water-signals"
```
Engines read governed facts and write derived facts — **no hardcoded taxonomy**: non‑complaint record kinds come from `water.record_kind` (`is_complaint=false`), domain grouping from `water.category.domain`, severity cut‑offs + window lengths from `water.config`. `water-hotspots` groups geocoded complaints by H3 and computes severity + `rate_per_10k`; `water-signals` computes reopen/recurring/emerging signals **and** writes one `water.overview` projection.

Each engine's `meta.yaml` declares the reads/writes (full versions in the Appendix), e.g. for `water-hotspots`:
```yaml
reads:  [water.complaint, water.record_kind, water.config]
writes: [water.hotspot]
scopes:
  - { verb: read, scope: water.complaint }
  - { verb: read, scope: water.record_kind }
  - { verb: read, scope: water.config }
  - { verb: write, scope: water.hotspot }
```

---

## 5. Build the dashboard — `water-dashboard`

```
new app water-dashboard
edit water-dashboard/meta.yaml
edit water-dashboard/dist/index.html   # paste Appendix → "water-dashboard"
```
A sandboxed browser app that reads governed data via `AOS.query(schema)` and uses the shared design system (`/shared/aos.css`, `/shared/aos.js`). It carries **zero** branding in code — labels come from `water.category`, everything else (title, agency, snapshot flag, thresholds) from `water.config`. Its `meta.yaml` must list every schema it reads:
```yaml
component_id: ds:airawatdeveloper:app/water-dashboard
kind: app
version: 0.1.0
provider: ds:airawatdeveloper:developer
display_name: Water Intelligence Dashboard
icon: 💧
entrypoint: index.html
bundle: dist
sandboxed: true
required_schemas: [water.overview, water.signal, water.hotspot, water.complaint, water.config, water.category]
```

---

## 6. Build the seed driver — `water-setup`

```
new driver water-setup
edit water-setup/meta.yaml            # writes: [water.category, water.record_kind, water.config]
edit water-setup/run.py               # paste Appendix → "water-setup" (full source)
```
This ships the **default taxonomy + configuration as data** and writes the three governed record types (idempotent; `RESET=1` overwrites). It's both your dev seed and the solution's install‑seed step. The `CATEGORIES`, `RECORD_KINDS` and `CONFIG` at the top of its `run.py` are the one place to tune the shipped defaults — all **data**, no logic.

---

## 7. Get your three data inputs

The pipeline needs three files. **This is the part newcomers miss — plan for it.**

**a) Complaint export (required).** Your utility's grievance CSV. Import it into your tenant's inbox:
```
fetch https://your-host/complaints.csv as /inbox/complaints.csv
```
(`fetch <url> as <path>` downloads a URL into your governed filestore; for large/local files use `aos upload`.) Note the path or the returned `file_id`.

**b) Geocoder gazetteer (needed for maps + hotspots).** A JSON list that maps each locality/PIN/ward in your city to a coordinate **and a precomputed H3 cell**, e.g. `{"localities":[["Name", lon, lat, "h3cell"], …], "pincodes":{…}, "wards":{…}}`. Build it once from open data (OSM place names + PIN centroids + a ward boundaries file). Import it and note its `file_id` → that's `GAZETTEER_FILE_ID`. *(Without it the driver still ingests complaints but can't place them on the grid, so there will be 0 hotspots.)*

**c) Ward population (needed for per‑capita `rate_per_10k`).** A `{"wards": {"<ward name>": <population>}}` lookup (e.g. WorldPop zonal sums per ward polygon). Import it and note its `file_id` → `POP_FILE_ID`. *(Optional — without it, hotspots still rank by raw volume.)*

> If you only have (a), you can still run the whole pipeline — you just won't get geospatial hotspots or per‑capita rates until you add (b) and (c).

---

## 8. Run the pipeline (in this order)

Order matters: seed first (so engines find the vocab/config), ingest, then analyze.
```
run water-setup
run water-complaints COMPLAINTS_FILE_ID=/inbox/complaints.csv GAZETTEER_FILE_ID=<id from 7b>
run water-hotspots POP_FILE_ID=<id from 7c>
run water-signals
```
✅ **Expected output (use `logs <component>` to see it):**
- `water-setup` → `wrote 15 records … water.category=11 water.record_kind=3 water.config=1`
- `water-complaints` → `record_kind: {…}` and `geocoded N/M complaints (…%)`
- `water-hotspots` → `wrote N water.hotspot … verify: N live … (expected N)`
- `water-signals` → `wrote N water.signal … overview: wrote 1 water.overview`

> **Gotcha:** a `run` in the sandbox is a **dev‑run** and does **not** inject values you set in **Settings** — only env on the run line (`KEY=VAL`) reaches it. (Once the solution is *installed*, the normal run path does read Settings.) Tip: `runs <component>` shows recent run status; `logs <component>` shows the last run's full output.

---

## 9. Open the dashboard

Launch **Water Intelligence Dashboard** from the desktop (or `/run?app=water-dashboard`). It renders from the governed facts + config. If a panel is empty, re‑run `water-signals` (it produces `water.overview`) and hard‑refresh.

---

## 10. Configure & re‑brand (edit data, not code)

To adapt the running solution for any utility you **edit the `water.config` record** — a *data* edit, not a source edit:
- In **Settings → Water Intelligence** (recommended), or
- From the Console, read/write the record `ds:<tenant>:water/config/default`.

Set `agency_name`, `dashboard_title`, `subtitle`, `is_live`, `zone_label`, `attributions`, `severity`, `windows`. To change the issue taxonomy, add/adjust **`water.category`** rows. To match a different CSV, point the driver's `*_FIELD` config at your column names. No redeploy, ever.

> Note: `edit water-<x>/run.py` edits *component source*; editing `water.config` edits *data*. Two different things — for re‑branding you want the data edit.

---

## 11. Publish to the marketplace (when ready)

1. **Ensure every schema is approved** by the AirawatNetwork (Network Admin → Schemas; this is the gate §2 mentioned). You can't publish with a pending schema.
2. `publish <component>` certifies + lists each component (needs a green run first).
3. `compose` bundles the project's published members into an installable **solution**.
4. An operator on another instance then follows the companion **Install Guide**.

---

## Appendix A — `aos` SDK mini‑reference

**In a driver/engine** (`run.py`), `from aos import driver, ingest, ctx`:
| Call | Does |
|---|---|
| `@driver.main` / `driver.run()` | marks + runs your `run()` entrypoint |
| `ctx.tenant` | the tenant this run writes into |
| `ctx.now()` | server timestamp (use for `observed_at`) |
| `ctx.facts("schema").list()` | read governed facts of a schema (returns rows; use `.get("object")`) |
| `ctx.write_many(rows, …)` | bulk‑write facts (batched via the broker) |
| `ctx.file.get(file_id)` | read a filestore blob (e.g. a gazetteer) |
| `ingest.table(schema, map_row, columns, file_env, …)` | CSV → facts: auto id, dedup, batched write |
| `ingest.crosswalk(canonical, env, keywords)` | build a label→code classifier (env override wins) |

**In an app** (`dist/index.html`), from `/shared/aos.js`:
| Call | Does |
|---|---|
| `AOS.boot(fn)` | init session/config, then run `fn` |
| `AOS.query(schema[, {filter,limit}])` | read governed facts under the signed‑in user |
| `AOS.esc`, `AOS.n`, `AOS.date` | escape / number / date helpers |

## Glossary
- **tenant** — isolated agency workspace. **sandbox/dev tenant** — relaxed rules for building.
- **registry** — the central governed store of all facts. **fact** — one validated record.
- **schema (noun)** — the record contract (`domain.subject`), governed by the Network.
- **driver / engine / app** — ingest‑or‑egress / compute‑over‑facts / browser‑UI component.
- **broker + confinement (L1)** — the clean‑room a driver/engine runs in; only I/O is `ctx`.
- **H3 (res‑9)** — hex grid cell (~0.1 km²) used to aggregate hotspots.
- **governed vocabulary / config** — reference data (`water.category`, `water.config`) that components read instead of hardcoding.
- **read‑back verify** — re‑reading your writes to confirm they persisted (schema‑rejected rows are dropped silently otherwise).


---

## Appendix B — For a team (multiple developers trying this)

Each teammate has their own login in `airawatdeveloper`. Whether they can follow this guide depends on **role** and on **name collisions**:

**1. They need a builder role — a bare login can view but not build.** Scaffolding/editing/running in the sandbox requires one of the Studio roles:
- `studio.product-manager` or `studio.project-manager` → **lead**: can `project new` (create a project) *and* build + submit to publish.
- `studio.developer` / `studio.business-analyst` / `studio.test-engineer` → can build **inside a project they're invited to**, but cannot start one.
- `platform.tenant-admin` → full admin, can do everything.

A tenant‑admin grants these (Admin app → the user → add role). A login with **no** role can still *open* a shelved app (e.g. view the finished dashboard) but every `new`/`edit`/`run` will be refused.

**2. Component ids are unique *per tenant* — so you can't all build `water-complaints` in the same tenant.** The reference solution already occupies `ds:airawatdeveloper:driver/water-complaints`, `engine/water-hotspots`, etc. Two good options:
- **Best — one sandbox tenant per learner** (e.g. `dev-asha`, `dev-ravi`). Fully isolated: each person builds the exact names in this guide, with their own data, no clashes. Ask your admin to provision them.
- **Shared tenant — use unique names.** If everyone must share `airawatdeveloper`, each learner suffixes their components (`water-complaints-asha`, `water-hotspots-asha`, `water-dashboard-asha`) and **skips §2** — the 7 `water.*` schemas are already governed in this instance, so reuse them (don't re‑propose; that collides).

**3. Projects are owner‑scoped; facts are tenant‑shared.** Each person's `project new` is visible only to them (or whoever they add to the project `team`). But governed facts (`water.complaint`, …) are shared across the whole tenant — so in a *shared* tenant, multiple people writing the same schema/ids overwrite each other. In separate tenants, everyone's data is independent.

**Recommendation:** for a true "everyone builds it themselves" workshop, give each developer a **lead role in their own sandbox tenant**. For "everyone explores the finished thing," a plain login with the dashboard shelved is enough.


---

## Appendix C — Reference code (copy & adapt)

The real sources. In an installed solution, open any with `edit water-<component>/run.py`.

### `water-setup/run.py` (full — the seed + config pattern)
```python
#!/usr/bin/env python3
"""water-setup — seeds the Water Intelligence solution's governed REFERENCE DATA so NO component hardcodes it:
  • water.category    — the water-issue taxonomy (code→label, domain bucket, normalisation keywords)
  • water.record_kind — the contact-type vocabulary (complaint|query|no_response_call; is_complaint flags the denominator)
  • water.config      — the Solution Configuration (agency branding + operational knobs) every member reads

Idempotent: deterministic ids, re-run safe. The shipped values are a sensible DEFAULT taxonomy + a REFERENCE config
(Delhi Jal Board) — an installer edits water.config for their agency and may add/adjust water.category rows as DATA.
Confined — only I/O is the SDK ctx.
"""
import os
from aos import driver, ctx

# ---- default water-issue taxonomy (code, label, domain, keywords, order). Keywords normalise a board's raw
#      labels to `code`; first match by `order` wins. Domains bucket the donut/grouping. Edit as DATA, not code.
CATEGORIES = [
    ("tanker_request",   "Water tanker required",       "tanker", ["tanker"], 1),
    ("sewer_overflow",   "Sewer blockage / overflow",   "sewer",  ["sewer", "overflow", "blockage", "drain", "logging", "garbage", "manhole cover"], 2),
    ("manhole",          "Manhole / cover",             "sewer",  ["manhole"], 3),
    ("contamination",    "Dirty / contaminated water",  "water",  ["dirty", "contamination", "quality", "muddy", "smell"], 4),
    ("low_pressure",     "Low pressure",                "water",  ["low pressure", "pressure"], 5),
    ("pipeline_leakage", "Water pipeline leakage",      "water",  ["pipeline", "leakage", "leak", "pipe"], 6),
    ("no_water",         "No water",                    "water",  ["no water", "short supply", "non supply", "dry", "supply"], 7),
    ("road_restoration", "Road restoration",            "water",  ["road", "restoration", "pit"], 8),
    ("billing",          "Billing",                     "water",  ["bill", "billing", "payment"], 9),
    ("new_connection",   "New connection",              "water",  ["new connection", "connection"], 10),
    ("other",            "Other",                       "water",  [], 99),
]
RECORD_KINDS = [
    ("complaint",        "Complaint",         True),
    ("query",            "Query",             False),
    ("no_response_call", "No response call",  False),
]
# REFERENCE Solution Configuration (edit water.config for YOUR agency — this is the one Delhi-specific record).
CONFIG = {
    "agency_name": "Delhi Jal Board",
    "dashboard_title": "Water Intelligence Dashboard",
    "subtitle": "from complaints to insight for better water management",
    "is_live": False,
    "snapshot_caveat": ("This is a historical snapshot, not a live feed. Period-over-period \"trend\" deltas are "
                        "intentionally omitted — the export does not span enough time to compute them honestly "
                        "(the within-window week-over-week is shown instead). Hotspots show association, not causation."),
    "zone_label": "operational zones",
    "attributions": {"complaints_source": "DJB grievance export",
                     "geocoder": "OSM-PBF + PIN + ward",
                     "population_source": "WorldPop 2020 constrained"},
    "severity": {"rate_warn": 150, "rate_crit": 300, "count_crit_pct": 0.10, "count_warn_pct": 0.30},
    "windows": {"window_days": 30, "week_days": 7, "max_weeks": 4, "min_count": 1, "min_chronic": 20},
}


@driver.main
def run():
    T = ctx.tenant
    if not T:
        print("FAIL: no run tenant."); return 1
    reset = os.environ.get("RESET", "0").strip() in ("1", "true", "yes")

    def _have(schema):
        try:
            return {(r.get("object") or r).get("id") for r in ctx.facts(schema).list()}
        except Exception:
            return set()

    now = ctx.now()
    cat_rows, rk_rows = [], []
    for code, label, domain, kws, order in CATEGORIES:
        cat_rows.append({"schema": "water.category", "id": "ds:%s:water/category/%s" % (T, code),
                         "code": code, "label": label, "domain": domain, "keywords": kws, "order": order})
    for code, label, is_c in RECORD_KINDS:
        rk_rows.append({"schema": "water.record_kind", "id": "ds:%s:water/record_kind/%s" % (T, code),
                        "code": code, "label": label, "is_complaint": is_c})
    cfg_row = dict(CONFIG, schema="water.config", id="ds:%s:water/config/default" % T)

    wrote = 0
    if reset:
        cw = ctx.write_many(cat_rows); rw = ctx.write_many(rk_rows); fw = ctx.write_many([cfg_row])
        wrote = (cw or 0) + (rw or 0) + (fw or 0)
    else:
        have_cat, have_rk, have_cfg = _have("water.category"), _have("water.record_kind"), _have("water.config")
        new_cat = [r for r in cat_rows if r["id"] not in have_cat]
        new_rk = [r for r in rk_rows if r["id"] not in have_rk]
        new_cfg = [cfg_row] if cfg_row["id"] not in have_cfg else []
        if new_cat: wrote += ctx.write_many(new_cat) or 0
        if new_rk: wrote += ctx.write_many(new_rk) or 0
        if new_cfg: wrote += ctx.write_many(new_cfg) or 0

    live_cat = len(_have("water.category")); live_rk = len(_have("water.record_kind")); live_cfg = len(_have("water.config"))
    print("water-setup: wrote %d records (RESET=%s). live now: water.category=%d water.record_kind=%d water.config=%d"
          % (wrote, reset, live_cat, live_rk, live_cfg))
    print("edit water.config (ds:%s:water/config/default) to set YOUR agency name / attributions / thresholds." % T)
    return 0 if (live_cat >= len(CATEGORIES) and live_rk >= len(RECORD_KINDS) and live_cfg >= 1) else 2


if __name__ == "__main__":
    raise SystemExit(driver.run())
```
### `water-complaints` — governed taxonomy classifier + ingest call
```python
CANONICAL = ["no_water", "low_pressure", "contamination", "tanker_request", "pipeline_leakage",
             "sewer_overflow", "manhole", "road_restoration", "billing", "new_connection", "other"]

# generic keyword classifier — the safety net when TAXONOMY_MAP has no entry. Ordered by specificity (first hit wins);
# water-domain generic, NOT board-specific. A board's explicit TAXONOMY_MAP always takes precedence.
KEYWORDS = [
    ("manhole",          ["manhole"]),
    ("tanker_request",   ["tanker"]),
    ("road_restoration", ["pit", "road construction", "road restoration", "road cutting"]),
    ("pipeline_leakage", ["pipe"]),
    ("sewer_overflow",   ["sewer", "overflow", "blockage", "drain", "logging", "garbage", "manhole cover"]),
    ("contamination",    ["contaminat", "dirty", "quality", "smell", "muddy", "colour", "color"]),
    ("low_pressure",     ["low pressure", "pressure"]),
    ("no_water",         ["no water", "short supply", "supply", "dry", "non supply"]),
    ("new_connection",   ["new connection", "new water connection"]),
    ("billing",          ["bill", "payment", "charge", "arrear", "refund"]),
]

# column auto-detection: env override first (e.g. CATEGORY_FIELD), else the first header matching a candidate.
COLUMNS = {
    "id":       [os.environ.get("ID_FIELD"),          "tracking no", "tracking_no", "complaint id", "complaint_id", "id", "grievance id"],
    "channel":  [os.environ.get("CHANNEL_FIELD"),     "call type", "channel", "source"],
    "category": [os.environ.get("CATEGORY_FIELD"),    "complaint type", "complaint_type", "category", "type"],
    "subcat":   [os.environ.get("SUBCATEGORY_FIELD"), "subcategory", "sub category", "sub-category"],
    "priority": [os.environ.get("PRIORITY_FIELD"),    "priority"],
    "status":   [os.environ.get("STATUS_FIELD"),      "status"],
    "reg_date": [os.environ.get("REGISTERED_FIELD"),  "registered date", "registered_date", "registered on", "created date", "complaint date"],
    "reg_time": [os.environ.get("REGISTERED_TIME_FIELD"), "registered time", "registered_time"],
    "closed":   [os.environ.get("CLOSED_FIELD"),      "closed at", "closed_at", "closed date", "resolved at", "closure date"],
    "reopens":  [os.environ.get("REOPENS_FIELD"),     "number of reopens", "reopens", "reopen count"],
}
# ADMIN_COLUMNS (level→column) declares the board's hierarchy; else auto-detect the common civic levels.
AUTO_ADMIN = {"colony": "colony", "ward": "ward", "ac": "a.c. name", "zone": "zone"}

```
```python
    # GOVERNED taxonomy wins: if water.category is seeded (water-setup), rebuild the classifier from it so the
    # code carries no board-specific taxonomy — the built-in CANONICAL/KEYWORDS is only the offline fallback.
    global classify
    try:
        _cats = sorted([(r.get("object") or r) for r in ctx.facts("water.category").list()],
                       key=lambda c: c.get("order", 999))
        _cats = [c for c in _cats if c.get("code")]
        if _cats:
            classify = ingest.crosswalk(canonical=[c["code"] for c in _cats], env="TAXONOMY_MAP",
                                        keywords=[(c["code"], c.get("keywords") or []) for c in _cats])
            print("[water-complaints] taxonomy: %d governed water.category rows" % len(_cats), flush=True)
    except Exception as _e:
        print("[water-complaints] governed taxonomy unavailable (%s) — using built-in default." % str(_e)[:80], flush=True)
```
### `water-signals` — governed vocab loader (no taxonomy in code)
```python
def _load_vocab():
    """Read the GOVERNED water vocabulary (seeded by water-setup) — no taxonomy is hardcoded here.
    Returns (noncomplaint record_kinds, {category_code: domain}, water.config dict). Absent vocab ⇒ generic
    behaviour (all records treated as complaints; every category → the 'water' bucket), said once in the log."""
    def _objs(schema):
        try:
            return [(r.get("object") or r) for r in ctx.facts(schema).list()]
        except Exception:
            return []
    rk = _objs("water.record_kind")
    noncomplaint = {o.get("code") for o in rk if o.get("code") and not o.get("is_complaint")}
    cat_domain = {o.get("code"): (o.get("domain") or "water") for o in _objs("water.category") if o.get("code")}
    cfgs = _objs("water.config")
    cfg = cfgs[0] if cfgs else {}
    if not rk:
        print("[water-signals] no water.record_kind vocabulary — treating every record as a complaint (run water-setup to seed).")
```
### `water-hotspots` — governed vocab + config, write + read-back verify
```python
    # governed vocab/config — no taxonomy hardcoded. NONCOMPLAINT from water.record_kind; severity cuts from water.config.
    def _objs(sc):
        try: return [(r.get("object") or r) for r in ctx.facts(sc).list()]
        except Exception: return []
    NONCOMPLAINT = {o.get("code") for o in _objs("water.record_kind") if o.get("code") and not o.get("is_complaint")}
    _wcfgs = _objs("water.config"); _sev = (_wcfgs[0].get("severity") if _wcfgs else {}) or {}
    CRIT_PCT = float(os.environ.get("CRIT_PCT", str(_sev.get("count_crit_pct", 0.10))))
    WARN_PCT = float(os.environ.get("WARN_PCT", str(_sev.get("count_warn_pct", 0.30))))
```
### `water-dashboard` — read governed facts + config in the browser
```javascript
async function main(){
  let ov,hs,sig,cats,cfgs;
  try{
    [ov,hs,sig,cats,cfgs] = await Promise.all([
      AOS.query('water.overview',{limit:5}), AOS.query('water.hotspot',{limit:2000}), AOS.query('water.signal',{limit:500}),
      AOS.query('water.category',{limit:100}), AOS.query('water.config',{limit:5}) ]);
  }catch(e){ document.getElementById('app').innerHTML='<div class="loading">Could not load data: '+E(e.message||e)+'</div>'; return; }
  // governed vocab + Solution Configuration — no labels/branding hardcoded in this app
  (cats||[]).forEach(c=>{ if(c.code){ CATLAB[c.code]=c.label||c.code; if(c.domain && !DOMLAB[c.domain]) DOMLAB[c.domain]=c.domain.replace(/\b\w/g,x=>x.toUpperCase()); }});
  WCFG = (cfgs||[])[0] || {};
```
