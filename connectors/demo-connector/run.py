#!/usr/bin/env python3
"""demo-connector — the smallest possible warm connector: serves `demo.external` with fixed rows so the
data-connectors S2b runtime-registry + discovery loop can be proven without a real upstream. In warm mode the
launcher sets AIRAWAT_CONNECTOR_MODE=serve and `connector.main()` starts the long-lived /read + /healthz server;
in one-shot mode it answers a single AIRAWAT_QUERY. No ctx.fetch, no secrets, no egress."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_common"))
sys.path.insert(0, "/_common")          # warm container: shared helpers (ids.py)
sys.path.insert(0, "/connector_sdk")    # warm container: isolated aos SDK bind
from aos import connector

# A tiny fixed dataset. Each row already carries `schema`; the resolver stamps provenance/freshness/history.
_ROWS = [
    {"id": "DMO-1", "schema": "demo.external", "ward": 42, "status": "open",   "subject": "water logging"},
    {"id": "DMO-2", "schema": "demo.external", "ward": 42, "status": "closed", "subject": "leak repaired"},
    {"id": "DMO-3", "schema": "demo.external", "ward": 7,  "status": "open",   "subject": "low pressure"},
]


@connector.read("demo.external")
def read_demo(q):
    rows = _ROWS
    ward = q.get("ward")                         # honour the one pushdown the source "supports"
    if ward is not None:
        rows = [r for r in rows if r.get("ward") == ward]
    return connector.page(rows[: q.limit], source_ref="ds:airawatdeveloper:connector/demo-connector")


if __name__ == "__main__":
    connector.main()
