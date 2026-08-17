# components

The **first-party non-UI components** — drivers, engines, models, connectors. Built on `sdk`, declared by manifest, certified against the specs, and published to the marketplace. Third parties publish their own; these are the reference set.

Any component can split to its own repo (the ecosystem model); grouped here to start.

## Kinds

| Kind | What it is | Examples |
|---|---|---|
| `driver` | ingests facts or produces derived cell.* views | satellite (AOD, precipitation, landcover), osm-vectors, finance-dummy |
| `engine` | analytics over registry data | access, flood, livability, diagnosis |
| `model` | a published inference method's implementation | pm25-fusion (ML), rainfall-runoff (physics) |
| `connector` | data-at-source adapter for an agency | kmc-connector |

## Suggested structure

```
drivers/<name>/
  run.py            # uses sdk only (no raw registry/socket access)
  meta.yaml         # manifest: kind, reads/writes/egress/secrets, pricing/SLA, inference-method
engines/<name>/
models/<name>/
connectors/<name>/
```

Depends on `sdk` + `domain-spec`. Note: the standardized inference **methods** are declared in the specs; a `model` here is an *implementation* of one (its `component_ref`).

## Reference: `engines/air-diagnosis` — the first engine (exercises `intelligence-spec` INT-0001)

A confined air-quality diagnosis engine — the reference `kind: engine`. It reads a pm2.5
`air.station_reading` fact and climbs the analytics ladder: a **descriptive** Analysis (current
band), and when unhealthy a **prescriptive** Analysis + a **Signal** + an advisory **Recommendation**.
It proves the intelligence layer end-to-end against the running kernel:

```bash
bash engines/air-diagnosis/demo.sh     # needs the wired stack (../docker-compose.yml) up
```

The demo does the whole certified supply chain — **certify → publish (with read+write scopes) →
install (cert verified point-of-use, run-token minted) → seed a fact → run the engine → verify the
Analyses + Signal landed**. Its guarantees, all checked live:

- **Confined (SPEC-0004):** the engine's *only* I/O is the broker; a write to an un-granted scope
  (`governance.decision`) returns `403 capability_denied` (default-deny).
- **Disciplined (INT-0001):** every Analysis cites its `method` + `uncertainty_ref`; the real
  registry-stored output validates against `intelligence.analysis`, and a method-less Analysis is
  rejected (INT-METHOD-001).
- **Never decides (INT-REC-001):** the engine only *offers* a Recommendation; an **Authority** admits
  it into a Decision (`POST /v1/decisions`) with a signed receipt.

`meta.yaml` declares `kind: engine`, `reads: [air.station_reading]`,
`writes: [intelligence.analysis, intelligence.signal]`, and the matching broker `scopes`. The tiny
HTTP client in `run.py` is a stand-in for the `sdk` broker/HTTP binding.
