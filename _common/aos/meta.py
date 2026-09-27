"""aos.meta — ONE canonical meta.yaml model: derive + validate.

Replaces the ≥3 divergent hand-rolled meta parsers in the tree. `derive()` builds a
complete meta dict from the code (the @engine.verb decorators + the reads/writes the
SDK observed at runtime), so a developer never hand-maintains `reads`/`writes`/`scopes`
— they are a function of what the code actually did. `validate()` is the single
gate `aos check` runs locally, before the network ever sees the component.
"""
import re
from . import engine, READS, WRITES, FILES

_NOUN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")            # <domain>.<subject>
_CID = re.compile(r"^ds:[a-z0-9-]+:(driver|engine|app|skill|agent|pipeline|solution)/[a-z0-9-]+$")
_IFACE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*@\d+\.\d+\.\d+$")  # domain.verb@x.y.z
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def derive(base):
    """base = {component_id, kind, version, provider, [display_name], [reads], [writes]}.
    Returns a full meta dict; reads/writes/scopes come from the code unless overridden."""
    verbs = engine.verbs()
    reads = sorted(set(base.get("reads") or []) | READS)
    writes = sorted(set(base.get("writes") or []) | WRITES)
    meta = dict(base)
    meta["reads"] = reads
    meta["writes"] = writes
    if verbs:
        meta["implements"] = sorted({v["implements"] for v in verbs.values() if v["implements"]})
        meta["verbs"] = {name: v["implements"] for name, v in verbs.items() if v["implements"]}
    # scopes are DERIVED, never hand-written (kills the reads/writes↔scopes duplication)
    meta["scopes"] = ([{"verb": "read", "scope": r} for r in reads] +
                      [{"verb": "write", "scope": w} for w in writes])
    # filestore use is DERIVED too — matches the platform's existing `files: [put|get]` meta convention
    # (e.g. doc-acquire), so the runtime grants the blob capability without a hand-maintained declaration.
    files = set(base.get("files") or []) | FILES
    if files:
        meta["files"] = sorted(files)
    return meta


def validate(meta):
    """Return a list of human-readable problems; empty list = valid. The single gate."""
    p = []
    cid = meta.get("component_id", "")
    if not _CID.match(cid):
        p.append(f"component_id '{cid}' must match ds:<tenant>:<kind>/<name>")
    if meta.get("kind") not in ("driver", "engine", "app", "skill", "agent", "pipeline", "solution"):
        p.append(f"kind '{meta.get('kind')}' is not a known component kind")
    if not _SEMVER.match(str(meta.get("version", ""))):
        p.append(f"version '{meta.get('version')}' must be semver x.y.z")
    for r in meta.get("reads", []):
        if not _NOUN.match(r):
            p.append(f"read scope '{r}' must be a governed noun <domain>.<subject>")
    for w in meta.get("writes", []):
        if not _NOUN.match(w):
            p.append(f"write scope '{w}' must be a governed noun <domain>.<subject>")
    verbs = meta.get("verbs", {})
    implements = set(meta.get("implements", []))
    for name, iface in verbs.items():
        if not _IFACE.match(iface):
            p.append(f"verb '{name}' → '{iface}' must be a governed interface domain.verb@x.y.z")
        elif iface not in implements:
            p.append(f"verb '{name}' → '{iface}' is not listed in implements[]")
    # scopes must be exactly the derivation of reads/writes (no drift)
    want = ([{"verb": "read", "scope": r} for r in meta.get("reads", [])] +
            [{"verb": "write", "scope": w} for w in meta.get("writes", [])])
    if meta.get("scopes") not in (None, want):
        p.append("scopes drift from reads/writes — let aos derive them (don't hand-edit)")
    return p


def to_yaml(meta):
    """Minimal deterministic YAML emit (enough for the canonical fields; no external dep)."""
    lines, order = [], ["component_id", "kind", "version", "provider", "display_name",
                        "implements", "verbs", "reads", "writes", "scopes", "files"]
    for k in order + [x for x in meta if x not in order]:
        if k not in meta:
            continue
        v = meta[k]
        if isinstance(v, dict):
            lines.append(f"{k}:")
            for kk, vv in v.items():
                lines.append(f"  {kk}: {vv}")
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            lines.append(f"{k}:")
            for it in v:
                lines.append("  - {" + ", ".join(f"{kk}: {vv}" for kk, vv in it.items()) + "}")
        elif isinstance(v, list):
            lines.append(f"{k}: [{', '.join(map(str, v))}]")
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines) + "\n"
