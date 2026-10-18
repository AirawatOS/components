# Water Intelligence — configuration

Water Intelligence is a **generic** water-utility solution: one certified build, N boards, differing only by
**per-tenant configuration** — NOT by code and NOT by any file in this repo.

## How a board configures it (the real path)

The `water-complaints` driver **declares** its config keys in `meta.yaml` (`config: [...]` —
`COMPLAINTS_FILE_ID, SOURCE, TAXONOMY_MAP, ADMIN_COLUMNS, MAX, …`). When a board (tenant) installs the solution,
its admin sets those values in the component's **Settings → Data Sources / Config** surface. Those values are
stored per-tenant as `platform.component_config` ({tenant, component, values}); secrets route to Vault and a chosen
file binds a `file_id`. At run time the scheduler/runtime injects them as env into the confined run — so each
board's run uses its own crosswalk, admin columns, and source. (A friendly board-admin form for this — the
**Water Intelligence Setup Console** — is the productized surface; see `../SETUP-CONSOLE.md`.)

## What the files here are

- `presets/*.example.json` — **worked examples**, one per reference board, showing what a board's config looks
  like (the taxonomy crosswalk from the board's own labels → the shared canonical taxonomy, and the board's admin
  hierarchy columns). They are **references to copy from / seed a preset**, NOT read by the driver or the install.
  `presets/djb.example.json` is Delhi Jal Board's example — the first board the solution was proven against.

Nothing in this directory is board-specific *code*. Adding a new board = a new per-tenant `platform.component_config`
(ideally authored through the Setup Console), optionally seeded from one of these example presets.
