"""Shared id-derivation helper (ADR-0063) — the ONE place a writer turns a natural key into a resolvable id.

The system id (`ds:<steward>:<kind>/<local>`) and the human/business identifier (case number, PID, CNR, UDISE)
are different things (see ADR-0063 §2c). When an entity has an authoritative natural key, DERIVE the `<local>`
segment from it — so the resolvable id is also human-readable AND deterministic (same natural key ⇒ same id ⇒
idempotent re-ingest) — and ALSO record every natural key in the governed `identifiers[]` block
(platform.identifier) so `resolve-by-identifier` can bridge natural-key ⇄ ds: id.

Usage in a driver:
    from ids import entity
    case = entity("kmc", "justice/case", "justice.case",
                  natural=[("filing_no", "CS/1234/2026", "ds:kmc:court/principal"),
                           ("cnr", "KLKM010012342026", "eCourts")],   # the FIRST drives the id
                  extra={"stage": "trial", "status": "open", ...})
    # → id  ds:kmc:justice/case/cs-1234-2026  ·  identifiers[] populated with both keys
"""
import re


def slug(value) -> str:
    """A URL-safe, stable local-id segment from a natural-key value: lowercased, runs of non-alphanumerics → a
    single hyphen, trimmed. 'CS/1234/2026' → 'cs-1234-2026'."""
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")


def object_id(tenant: str, kind_path: str, value) -> str:
    """The resolvable ds: id with `<local>` DERIVED from a natural key. `kind_path` is the `<kind>/<subpath>`
    (e.g. 'justice/case'); `value` is the primary natural key. → ds:<tenant>:<kind_path>/<slug(value)>."""
    return f"ds:{tenant}:{kind_path}/{slug(value)}"


def identifiers(natural) -> list:
    """Build the governed identifiers[] block. `natural` = list of (scheme, value[, authority]) tuples."""
    out = []
    for it in natural:
        scheme, value = it[0], it[1]
        rec = {"scheme": scheme, "value": str(value)}
        if len(it) > 2 and it[2]:
            rec["authority"] = it[2]
        out.append(rec)
    return out


def entity(tenant: str, kind_path: str, schema: str, natural, extra=None) -> dict:
    """Assemble an entity object whose `id` is derived from its PRIMARY natural key (natural[0]) and whose
    `identifiers[]` carries all natural keys. `extra` supplies the domain fields. Returns the object dict."""
    if not natural:
        raise ValueError("entity() needs at least one natural identifier to derive the id")
    obj = {"schema": schema, "id": object_id(tenant, kind_path, natural[0][1]),
           "identifiers": identifiers(natural)}
    if extra:
        obj.update(extra)
    return obj
