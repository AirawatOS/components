# Install "Water Intelligence" on your AirawatOS instance

This guide is for an **operator / tenant‑admin** installing the packaged **Water Intelligence** solution from the marketplace onto their own AirawatOS instance, and bringing it live with their utility's own data. No code changes are required — the solution is fully configured through governed data.

---

## What you get

A grievance‑first water operations solution:
- **Ingest** — `water-complaints` turns your complaint export (CSV) into geocoded `water.complaint` facts.
- **Analysis** — `water-hotspots` (hot H3 cells, severity, per‑capita rate) and `water-signals` (reopen / recurring / emerging + an overview projection).
- **Dashboard** — `water-dashboard`, a read‑only operations board.
- **Governed vocabulary + configuration** seeded by `water-setup`: `water.category`, `water.record_kind`, `water.config`.

All 7 `water.*` schemas are governed by the AirawatNetwork (network‑approved), so the solution installs the same way on any conformant instance.

---

## 1. Install the solution

From the Marketplace (or the Console):
```
install ds:airawat:solution/water-intelligence
```
Installing fans out to install every member component (driver, engines, app) and shelves the dashboard. Requires a **tenant‑admin**.

## 2. Seed the reference data + configuration

```
run water-setup
```
This writes the default taxonomy (`water.category`), contact‑type vocabulary (`water.record_kind`), and the Solution Configuration (`water.config`). Idempotent — re‑run anytime; `run water-setup RESET=1` restores defaults.

## 3. Configure for your agency

Edit the single `water.config` record (`ds:<your‑tenant>:water/config/default`) — in **Settings → Water Intelligence**, or via the Console — and set:
- `agency_name`, `dashboard_title`, `subtitle`
- `is_live` (false shows the "snapshot" caveat; set true once you have a live feed)
- `zone_label` (what you call your operational zones)
- `attributions` — `{complaints_source, geocoder, population_source}` shown in Data Sources
- `severity` — `{rate_warn, rate_crit, count_crit_pct, count_warn_pct}`
- `windows` — `{window_days, week_days, max_weeks, min_count, min_chronic}`

If your complaint types differ from the defaults, add/adjust **`water.category`** rows (code, label, domain bucket, matching keywords). Everything the dashboard shows is driven by these two record types.

## 4. Provide your data (three inputs)

**a) Complaint export (required).** Import your CSV:
```
fetch https://…/complaints.csv as /inbox/complaints.csv       # or: aos upload
```
The ingest driver **auto‑detects** common columns from the header. If yours differ, set the mapping as component config (Settings → water-complaints, or on the run line): `CATEGORY_FIELD`, `STATUS_FIELD`, `REGISTERED_FIELD`, `CLOSED_FIELD`, `REOPENS_FIELD`, `RECORD_KIND_FIELD`, …

**b) Geocoder gazetteer (for maps/hotspots).** A locality/PIN/ward → coordinate file for your city (each entry carries a precomputed H3 cell). Import it and note its `file_id`.

**c) Ward population (for per‑capita `rate_per_10k`).** A `{ward: population}` lookup for your wards (e.g. from WorldPop zonal sums). Note its `file_id`.

## 5. Run the pipeline

```
run water-complaints COMPLAINTS_FILE_ID=/inbox/complaints.csv GAZETTEER_FILE_ID=<gazetteer id>
run water-hotspots  POP_FILE_ID=<ward-population id>
run water-signals
```
Each run reads back its writes and prints a live count. Expect, e.g. `water-complaints` → "geocoded N complaints (…%)", `water-hotspots` → "wrote N water.hotspot … verify: N live", `water-signals` → "wrote N water.signal … overview: wrote 1 water.overview".

(You can set these ids once as component config so future runs are just `run water-complaints`.)

## 6. Open the dashboard

Launch **Water Intelligence Dashboard** from the desktop (or `/run?app=water-dashboard`). It renders your agency's branding, categories and zones from the config you set in step 3.

---

## Honesty & scope notes (by design)

- The dashboard distinguishes a **historical snapshot** from a **live feed** (`water.config.is_live`). With a short snapshot, period‑over‑period "trend" deltas are intentionally omitted — a within‑window week‑over‑week is shown instead. Set `is_live:true` only when you have an ongoing feed.
- Hotspots show **association, not causation**. Complaint volume is a reported proxy.
- Per‑capita `rate_per_10k` requires the ward‑population input; without it, hotspots still rank by volume.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard panel empty | `run water-signals` (generates `water.overview`); hard‑refresh. |
| Dashboard shows generic labels | `run water-setup` (seeds `water.category`/`water.config`). |
| 0 hotspots after ingest | complaints lack `h3` → provide a `GAZETTEER_FILE_ID` and re‑run `water-complaints`. |
| Categories all "other" | add keyword rows to `water.category`, or set `CATEGORY_FIELD` to the right column. |
| `rate_per_10k` missing | provide `POP_FILE_ID` to `water-hotspots`. |

## Upgrading / re‑branding later

Re‑branding is a data edit (`water.config`), never a redeploy. New solution versions install over the old via the Marketplace; your `water.config` and `water.category` edits persist as tenant data.
