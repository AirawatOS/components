"""ADR-0109 S2b (graph side) — materialize the traceability graph as governed `platform.trace_link` edges.

The service_spec instance carries `impl`/`traces` inline (good for a quick read). S2b promotes those to
FIRST-CLASS registry edges so the chain composes platform-wide and points at real governed component ids:

    (spec element) --implemented_by--> (component, + code locator)
    (acceptance)    --verifies-------> (business rule)

Why a dedicated schema and not `prov.derivation`: prov.derivation (ADR-0041, and not even governed yet) models
DATA-record lineage (`inputs[].ref+version → output`). Requirement→code→test traceability is a different edge;
`platform.trace_link` is the clean primitive for it. The two are complementary.

Run inside app-host (steward/airawat, in-container endpoints):
    docker exec -e IDN=http://identity:8093 -e NET=http://network:8090 -e KERNEL=http://kernel:8080 \
        airawat-app-host python3 write_trace_links.py
"""
import json, os, urllib.request, urllib.error, http.cookiejar

APP = os.environ.get("APP", "http://localhost:8099")
NET = os.environ.get("NET", "http://network:8090")
IDN = os.environ.get("IDN", "http://identity:8093")
KERNEL = os.environ.get("KERNEL", "http://kernel:8080")
SPEC_ID = "ds:airawat:service_spec/property-tax"
ID_PAT = r"^ds:[a-z0-9-]+:\S+$"
SID = "platform.trace_link"

def post(url, body, tok=None, cj=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try:
        return json.loads(op.open(req, timeout=20).read() or b"{}"), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code} {e.read().decode()[:200]}"

STOK = (post(IDN + "/realms/airawat/login", {"username": "steward"})[0] or {}).get("access_token")
cj = http.cookiejar.CookieJar()
post(APP + "/login", {"realm": "airawat", "username": "steward"}, cj=cj)

# --- 1. govern platform.trace_link ---
S = {"type": "string"}; Sn = {"type": ["string", "null"]}
defn = {"type": "object", "x-kind": "record", "additionalProperties": False,
        "required": ["schema", "id", "subject", "relation", "object"], "title": SID,
        "description": ("A TRACEABILITY EDGE (ADR-0109 S2): links a service-spec element to the governed object "
                        "that realizes or verifies it. `subject`→`relation`→`object` builds the "
                        "requirement→rule→interface→code→test graph; queryable for 'which code implements rule X' "
                        "and 'which requirement has no test'."),
        "properties": {"schema": {"const": SID}, "id": {"type": "string", "pattern": ID_PAT},
                       "subject": S, "relation": {"enum": ["implemented_by", "verifies", "tests", "refines", "depends_on"]},
                       "object": S, "locator": Sn, "spec": Sn, "status": Sn,
                       "tenant": Sn, "created_by": Sn, "created_at": Sn}}
pr, pe = post(APP + "/bridge/schema-request",
              {"schema_id": SID, "version": "0.1.0", "definition": defn, "x_kind": "record",
               "justification": "ADR-0109 S2: first-class traceability edges for the service-spec graph."}, cj=cj)
print("propose platform.trace_link:", "OK" if not pe else pe)
dr, de = post(NET + "/v1/schema-requests/decide", {"schema_id": SID, "version": "0.1.0", "decision": "approve"}, tok=STOK)
print("approve platform.trace_link:", (dr.get("published_schema") if dr else "ERR " + str(de)))

# --- 2. load the spec + compute edges ---
res = post(KERNEL + "/v1/registry/resolve", {"id": SPEC_ID}, tok=STOK)[0]
spec = ((res.get("results") or [{}])[0].get("object") or {}).get("spec") or {}

ENGINES = {"tax-gap-engine": "ds:airawatdeveloper:engine/tax-gap-engine",
           "campaign-engine": "ds:airawatdeveloper:engine/campaign-engine"}
def resolve_impl(impl):
    """map a 'tax-gap-engine/run.py:270' locator → (component_id, locator)."""
    if not impl:
        return None, None
    head = impl.split("/", 1)[0]
    if head in ENGINES:
        return ENGINES[head], impl.split("/", 1)[1]
    return impl, None

edges = []
for o in spec.get("operations", []):
    cid, loc = resolve_impl(o.get("impl"))
    if cid: edges.append((f"{SPEC_ID}#op/{o['verb']}", "implemented_by", cid, loc, o.get("status")))
for r in spec.get("business_rules", []):
    cid, loc = resolve_impl(r.get("impl"))
    if cid: edges.append((f"{SPEC_ID}#rule/{r['id']}", "implemented_by", cid, loc, r.get("status")))
for a in spec.get("acceptance_criteria", []):
    for t in (a.get("traces") or []):
        edges.append((f"{SPEC_ID}#ac/{a['id']}", "verifies", f"{SPEC_ID}#rule/{t}", None, a.get("status")))

# --- 3. write each edge as a governed platform.trace_link record (steward) ---
ok = 0
for i, (subj, rel, obj, loc, st) in enumerate(edges):
    eid = f"ds:airawat:trace_link/property-tax-{i:03d}"
    rec = {"schema": SID, "id": eid, "subject": subj, "relation": rel, "object": obj,
           "locator": loc, "spec": SPEC_ID, "status": st}
    _, e = post(KERNEL + "/v1/registry/objects", {"id": eid, "object": rec}, tok=STOK)
    if e: print("  edge ERR", eid, e)
    else: ok += 1
print(f"wrote {ok}/{len(edges)} trace_link edges")

# --- 4. graph query read-back: resolve each edge by id (resolve-by-schema is not a server-side filter) ---
rows = []
for i in range(len(edges)):
    rr = post(KERNEL + "/v1/registry/resolve", {"id": f"ds:airawat:trace_link/property-tax-{i:03d}"}, tok=STOK)[0] or {}
    rs = rr.get("results") or []
    if rs:
        rows.append(rs[0].get("object") or rs[0])
impl = [r for r in rows if r.get("relation") == "implemented_by"]
ver = [r for r in rows if r.get("relation") == "verifies"]
print(f"graph holds {len(rows)} trace_link edge(s) · implemented_by: {len(impl)} · verifies: {len(ver)}")
foot = next((r for r in rows if r.get("subject", "").endswith("#rule/PT-GAP-FOOTPRINT")), None)
if foot:
    print(f"  Q: which code implements PT-GAP-FOOTPRINT? -> {foot['object']} ({foot.get('locator')})")
