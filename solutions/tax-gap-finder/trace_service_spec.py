"""ADR-0109 S2 (query side) — traceability queries over a governed platform.service_spec.

The Service Specification instance already carries, per element, its `impl` id (the governed code that
realizes it) and its `test` (the conformance test, or null). This tool resolves the governed instance from
the registry and answers the two canonical questions as QUERIES, not audits:

    python3 trace_service_spec.py untested                 # which acceptance criteria have no test?
    python3 trace_service_spec.py implements PT-GAP-FOOTPRINT  # what code implements this element?
    python3 trace_service_spec.py coverage                 # built vs roadmap rollup
    python3 trace_service_spec.py orphans                  # rules with no acceptance criterion
    python3 trace_service_spec.py                          # run all of the above (demo)

S2 (graph side, next): emit each element→impl as a governed `prov.derivation` edge so the chain
(requirement → rule → interface → code → test → deployment) is a first-class registry graph that composes
across specs — see emit_edges() below (prints the edges it WOULD write; writing is the follow-up increment).

Endpoints default to in-container names (run inside app-host); override with IDN/KERNEL env.
"""
import json, os, sys, urllib.request

IDN = os.environ.get("IDN", "http://identity:8093")
KERNEL = os.environ.get("KERNEL", "http://kernel:8080")
SPEC_ID = os.environ.get("SPEC_ID", "ds:airawat:service_spec/property-tax")

def post(url, body, tok=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST"), timeout=15).read() or b"{}")

def load_spec():
    tok = post(IDN + "/realms/airawat/login", {"username": "steward"})["access_token"]
    res = post(KERNEL + "/v1/registry/resolve", {"id": SPEC_ID}, tok)
    results = res.get("results") or []
    if not results:
        print("no such service_spec:", SPEC_ID); sys.exit(1)
    obj = results[0].get("object") or results[0]
    return obj, obj.get("spec") or {}

def _elements(spec):
    """Flatten the spec into (node_id, kind, status, impl, test, traces) tuples."""
    out = []
    for n in spec.get("domain_model", []):
        out.append((f"{SPEC_ID}#noun/{n['noun']}", "noun", n.get("status"), n.get("impl"), None, []))
    for o in spec.get("operations", []):
        out.append((f"{SPEC_ID}#op/{o['verb']}", "operation", o.get("status"), o.get("impl"), None, []))
    for r in spec.get("business_rules", []):
        out.append((f"{SPEC_ID}#rule/{r['id']}", "rule", r.get("status"), r.get("impl"), None, []))
    for a in spec.get("acceptance_criteria", []):
        out.append((f"{SPEC_ID}#ac/{a['id']}", "acceptance", a.get("status"), None, a.get("test"), a.get("traces") or []))
    return out

def untested(spec):
    rows = [a for a in spec.get("acceptance_criteria", []) if a.get("test") is None]
    print(f"acceptance criteria with NO test: {len(rows)}/{len(spec.get('acceptance_criteria', []))}")
    for a in rows:
        print(f"  - {a['id']}  (status={a.get('status')}, traces={a.get('traces') or []})")

def implements(spec, q):
    hits = [(nid, kind, impl) for (nid, kind, st, impl, test, tr) in _elements(spec) if impl and q.lower() in nid.lower()]
    if not hits:
        print(f"no implemented element matches '{q}'")
    for nid, kind, impl in hits:
        print(f"  {kind:11s} {nid.split('#')[-1]:28s} → {impl}")

def coverage(spec):
    els = _elements(spec)
    for kind in ("noun", "operation", "rule", "acceptance"):
        g = [e for e in els if e[1] == kind]
        b = sum(1 for e in g if e[2] == "built"); rm = sum(1 for e in g if e[2] == "roadmap")
        print(f"  {kind:11s} {len(g):2d} total | {b} built | {rm} roadmap")
    acs = spec.get("acceptance_criteria", [])
    print(f"  tests: 0/{len(acs)} acceptance criteria have a conformance test")

def orphans(spec):
    traced = set()
    for a in spec.get("acceptance_criteria", []):
        traced.update(a.get("traces") or [])
    rules = [r["id"] for r in spec.get("business_rules", [])]
    orph = [r for r in rules if r not in traced]
    print(f"business rules with NO acceptance criterion: {len(orph)}/{len(rules)}")
    for r in orph:
        print(f"  - {r}")

def emit_edges(spec):
    """S2 graph side (NEXT): the prov.derivation edges this spec implies. Printed, not yet written."""
    edges = []
    for (nid, kind, st, impl, test, tr) in _elements(spec):
        if impl:
            edges.append({"subject": nid, "relation": "implemented_by", "object": impl, "status": st})
        for t in tr:
            edges.append({"subject": nid, "relation": "verifies", "object": f"{SPEC_ID}#rule/{t}"})
    print(f"prov.derivation edges implied by this spec: {len(edges)} (writing them is the S2b increment)")
    for e in edges[:6]:
        print("  ", e)
    print("   …")

if __name__ == "__main__":
    obj, spec = load_spec()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    print(f"service_spec: {obj.get('id')}  (capability: {(obj.get('capability') or '')[:48]})\n")
    if cmd in ("untested", "all"): print("== untested =="); untested(spec); print()
    if cmd in ("coverage", "all"): print("== coverage =="); coverage(spec); print()
    if cmd in ("orphans", "all"): print("== orphan rules =="); orphans(spec); print()
    if cmd == "implements": print(f"== implements '{arg}' =="); implements(spec, arg)
    elif cmd == "all": print("== implements 'PT-GAP' =="); implements(spec, "PT-GAP"); print(); print("== prov edges (preview) =="); emit_edges(spec)
