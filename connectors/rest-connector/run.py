#!/usr/bin/env python3
"""rest-connector — resolve a governed schema LIVE from a REST system of record (data-connectors S0/S1).

Implements data.read@1.0: the kernel resolver sends a governed query, this connector translates it into the
source API's native call, fetches, and maps the response back into the governed schema's shape. Config-driven
(PARAM_MAP / FIELD_MAP) so one certified component serves many sources. Confined: the only I/O is the declared
egress via ctx.fetch (Vault key injected by the broker). Reads only.

Capability contract: this source honours eq/in/gte/lte filters and cursor paging but cannot sort or project —
the connector pushes down what it can, marks the rest so the RESOLVER compensates, and reports completeness so a
partial answer is never mistaken for a complete one.
"""
import os, json
from aos import connector, ctx

_SOURCE = os.environ.get("SOURCE_URL", "").rstrip("/")
_PATH   = os.environ.get("PATH_", os.environ.get("PATH_OVERRIDE", "")) or os.environ.get("REST_PATH", "/complaints")
_PARAM  = json.loads(os.environ.get("PARAM_MAP") or "{}")   # governed field -> source query-param
_FIELD  = json.loads(os.environ.get("FIELD_MAP") or "{}")   # governed field -> source response-field
_KEY    = os.environ.get("KEY_FIELD", "id")
_PUSHABLE = {"eq", "in", "gte", "lte"}                       # must match meta capabilities.filter_ops


def _map_row(src):
    """Map one source row into the governed schema's shape using FIELD_MAP (identity reversed: governed<-source)."""
    out = {}
    for gov_field, src_field in (_FIELD or {}).items():
        if src_field in src:
            out[gov_field] = src.get(src_field)
    # fall through: if no FIELD_MAP, pass the row as-is (source already governed-shaped)
    return out or dict(src)


@connector.read("water.complaint")
def read(q):
    # 1. split the query: push down only the predicates this source supports; the rest is residual (resolver does it)
    params, residual = {}, []
    for w in q.where:
        f, op, v = w.get("field"), w.get("op", "eq"), w.get("value")
        if op in _PUSHABLE and f in _PARAM:
            params[_PARAM[f]] = ",".join(map(str, v)) if isinstance(v, (list, tuple)) else v
        else:
            residual.append(w)                       # resolver compensates (filter it couldn't push)
    if q.cursor:
        params["cursor"] = q.cursor
    params["limit"] = min(q.limit or 100, 200)

    # 2. call the source (brokered egress; Vault key injected). ctx.fetch records provenance.
    url = _SOURCE + _PATH + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
    raw = ctx.fetch(url)                             # returns a FetchResult (.json()/.body/.ok)
    if hasattr(raw, "json"):                          # FetchResult (live + in-memory test runtimes)
        body = raw.json()
    elif isinstance(raw, dict):
        body = raw
    else:
        body = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    items = body.get("items") or body.get("results") or body.get("data") or (body if isinstance(body, list) else [])
    rows = [_map_row(r) for r in items]

    # 3. report completeness honestly: if we couldn't push a filter, the page is PARTIAL — the resolver must
    #    finish it (and may refuse if the caller required completeness and the source can't bound the set).
    cursor = body.get("next") or body.get("cursor")
    if residual:
        return connector.partial(rows, reason="source_filter_unsupported",
                                  evaluated_rows=len(rows), cursor=cursor,
                                  source_ref="ds:airawatdeveloper:ref/source/djb-grievance")
    return connector.page(rows, cursor=cursor,
                          source_ref="ds:airawatdeveloper:ref/source/djb-grievance")


if __name__ == "__main__":
    connector.run()
