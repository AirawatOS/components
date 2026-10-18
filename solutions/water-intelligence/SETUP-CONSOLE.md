# Water Intelligence — Setup Console (scope)

**Problem.** A board installs the generic Water Intelligence solution and must configure it to its own data —
today that means typing env keys (incl. big JSON maps like `TAXONOMY_MAP`) into the generic Settings → Config
field, or copying from an example preset. That's fine for a developer, wrong for a water-board admin.

**Goal.** A friendly, board-admin-facing **Setup Console** app (member of the solution) that turns the driver's
declared config contract into a guided form and writes the tenant's own `platform.component_config` — no JSON,
no Studio. One build, N boards, each self-configured.

## Where it writes (reuses existing plumbing — no new storage)
- **Per-tenant values** → `platform.component_config` {tenant, component:`…/water-complaints`, values} via the
  existing config/setup bridge (ADR-0092). Injected as env into each run by the scheduler/runtime.
- **Source file** → filestore upload → `file_id` bound to `COMPLAINTS_FILE_ID` (Settings asset-binding flow).
- **Cadence** → the auto-created `platform.schedule` (edit cron / pause) via `/bridge/schedule`.
- **Run now / status** → the existing run path (`/bridge/run`) + `/bridge/runs`.

## The form (grouped, over the driver's declared `config:` contract — generic, not hardcoded)
1. **Source** — upload the complaints export (→ `COMPLAINTS_FILE_ID`) + a `SOURCE` label. (Future: a live
   endpoint `SOURCE_URL`/SFTP once the driver gains egress pull.)
2. **Column mapping** — auto-detect columns from the uploaded file's header; let the admin confirm/override the
   `*_FIELD` keys (id / category / status / registered / closed / reopens).
3. **Taxonomy crosswalk** — two-column editor: each of the board's raw complaint labels → a **canonical category**
   dropdown. The canonical list (`no_water, low_pressure, contamination, …`) comes from the GOVERNED
   `water.complaint` schema enum / `ref.domain` vocab — not hardcoded in the app. Writes `TAXONOMY_MAP`.
4. **Admin hierarchy** — map the board's admin columns (its names for colony/ward/zone/…) → `ADMIN_COLUMNS`.
5. **SLA + schedule** — default `sla_days` per category; the ingest cron (reuse the schedule control).
6. **Preset** — optionally start from a shipped example preset (DJB / Nashik from `config/presets/*.example.json`)
   to pre-fill 1–5, then tweak. Presets are seeds, never the live config.

A **"Run ingest now"** button + a last-run panel (rows written new/changed, % mapped, PII-dropped=0) so the admin
sees it work before trusting the nightly schedule.

## Slices
- **S1 — shell + simple keys: ✅ BUILT** (`components/apps/water-setup`, app `ds:airawatdeveloper:app/water-setup`).
  Resolves the installed water-complaints driver (via `/bridge/settings` list), reads its config contract + current
  values, and gives a 3-step form: ① Source — file upload (`/bridge/file-upload`, ≤25 MB) → `COMPLAINTS_FILE_ID` +
  `SOURCE` + `MAX`, saved via `/bridge/settings-set` (→ platform.component_config); ② Mapping — `TAXONOMY_MAP` +
  `ADMIN_COLUMNS` JSON (S2 replaces with the visual editor + ✨ Auto-map); ③ Run — `/bridge/run` + recent runs
  (`/bridge/runs`). Admin-gated (board admin). On the tenant shelf. Caveats: 25 MB upload cap (full historical file
  via CLI / chunked upload later); new app serves from the overlay + shelf — a proper Studio Publish registers it in
  the certified set for role-gated serving across tenants.
- **S2 — taxonomy crosswalk editor:** label→canonical two-column UI; canonical list from the governed schema;
  writes `TAXONOMY_MAP`. Seedable from a preset. **✨ Auto-map with AI** — the console sends the board's DISTINCT
  labels + the canonical list to kimi-k3 (ONE call over the vocabulary, not per row) via `POST /bridge/suggest-crosswalk`
  {labels[], canonical[], hint} → {map, unmapped}; the admin reviews/edits, then saves as config. (Same logic is
  available to a driver author as `aos.ingest.suggest_crosswalk(labels, canonical)`.) BUILT + proven: live kimi mapped
  all 21 real DJB labels sensibly. Ingest stays deterministic exact-match — the LLM only proposes the config.
- **S3 — column auto-detect + admin hierarchy:** parse the uploaded header; map `*_FIELD` + `ADMIN_COLUMNS`.
- **S4 — presets + cadence:** preset picker (DJB/Nashik) to pre-fill; cron control folded in.
- **S5 (future) — live source:** `SOURCE_URL`/SFTP/API config once the driver supports egress pull.

## Acceptance
A non-technical board admin, immediately after installing the solution, completes setup entirely in this console
(no JSON, no Studio), clicks Run ingest, sees their complaints land in `water.complaint` with their own crosswalk
and admin mapping, and the nightly schedule then keeps it current. Zero board-specific code; a second board is
setup-only.

## Non-goals
- Not the developer build surface (that's Studio). - Not a replacement for governance (schemas/roles stay
governed). - Not where secrets live (Vault, via the same config flow).
