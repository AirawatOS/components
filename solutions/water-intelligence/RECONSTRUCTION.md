# Water Intelligence — component reconstruction (2026-10-23)

The `water-hotspots` and `water-signals` **engines** were built via Studio in the `airawatdeveloper` tenant and
dev-run (proven by `metering.usage/run-dev-*`), but their source lived only in the ephemeral app-host
`/app/components` dir — never published as a `platform.component_asset`, never committed — so it was lost on an
app-host restart. `water-dashboard`'s source was likewise only in the live app-bundle overlay. Only the engines'
**output** survived in the registry.

These components were **reconstructed from the governed build-guide + the recorded output as an oracle**, and
verified to regenerate the recorded records given the same inputs:

| Component | Status | Verification vs the recorded airawatdeveloper / DJB data |
|---|---|---|
| `engine/water-hotspots` | reconstructed | 350/350 hotspots — `count`, `dominant`, `category`, `ward`, `zone`, `severity`, `window`, `id` all exact; `rate_per_10k` ward-level reproduces given the ward-population asset (345/350, rest = pop-rounding); `wow_change` re-baselined (last-7d/prior-7d ratio). |
| `engine/water-signals` | reconstructed | 43/48 signals byte-identical; the other 5 differ only in the recurring-cell `ward` *label* (geometric `space.ward_cell` vs complaint-modal — metric/selection/id exact). `water.overview`: every total + all 6 breakdowns + the 34-day trend identical. |
| `app/water-dashboard` | recovered | restored verbatim from the live app bundle (20.8 KB). |
| `driver/water-docs` | reconstructed | seeds the Build/Install guides into Docs (the draft was never persisted). |
| `driver/water-complaints`, `driver/water-setup` | already in git | unchanged. |
| 7 `water.*` schemas | captured | the live governed definitions, saved under `schemas/`. |

Derived logic (now the canonical spec, grounded in the recorded data):
- **hotspots window** = trailing 30 days INCLUSIVE `[max_date-29 .. max_date]`; candidates = complaint-kind +
  geocoded + in window; severity = count percentile (top 10% critical, next 20% warning, else info).
- **signals** anchor = max registered date; emerging = last-7d vs prior-7d by category/citywide; recurring =
  chronic cells present in all 4 trailing weeks (top-N by 28-day count) + chronic_share citywide; reopen =
  reopened(reopens>=1) of complaints by category/ward(top-N)/citywide.
- `config/ward-population.delhi.json` — the ward populations (recovered from the recorded rates) that
  water-hotspots consumes via `POP_FILE_ID` to reproduce `rate_per_10k`.

⚠️ Durability: these are committed to git AND must be published as `platform.component_asset` (filestore) so they
materialise for Console/Studio and survive an app-host restart — NEVER left only in a baked image / the ephemeral
components dir. See memory `persist-generated-code-rule`.
