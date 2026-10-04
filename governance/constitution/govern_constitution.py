"""ADR-0109 S3 — govern `platform.constitution` + write the `default` instance.

The constitution is the persistent rule-set every generation agent inherits and every component is certified
against. This governs the schema (light envelope + open `principles` body + authored `source_yaml`) and writes
`ds:airawat:constitution/default`, honestly statusing each principle (enforced / partial / advisory). The
service_spec already references this id, so this closes that link.

Run inside app-host (steward/airawat, in-container endpoints):
    docker exec -e APP=http://localhost:8099 -e NET=http://network:8090 -e IDN=http://identity:8093 \
        -e KERNEL=http://kernel:8080 airawat-app-host python3 govern_constitution.py
"""
import json, os, pathlib, urllib.request, urllib.error, http.cookiejar

APP = os.environ.get("APP", "http://localhost:8099")
NET = os.environ.get("NET", "http://network:8090")
IDN = os.environ.get("IDN", "http://identity:8093")
KERNEL = os.environ.get("KERNEL", "http://kernel:8080")
ID_PAT = r"^ds:[a-z0-9-]+:\S+$"
SID = "platform.constitution"
CID = "ds:airawat:constitution/default"

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

# --- 1. govern platform.constitution ---
S = {"type": "string"}; Sn = {"type": ["string", "null"]}
defn = {"type": "object", "x-kind": "record", "additionalProperties": False,
        "required": ["schema", "id", "title"], "title": SID,
        "description": ("THE CONSTITUTION (ADR-0109 S3): the persistent rules every generation agent inherits "
                        "and every component is certified against. `principles[]` carry statement + enforce{mode,by} "
                        "+ status (enforced/partial/advisory); `source_yaml` is the authored source."),
        "properties": {"schema": {"const": SID}, "id": {"type": "string", "pattern": ID_PAT},
                       "title": S, "version": Sn, "status": Sn,
                       "principles": {"type": "array", "items": {"type": "object"}},
                       "source_yaml": Sn, "tenant": Sn, "created_by": Sn, "created_at": Sn}}
pr, pe = post(APP + "/bridge/schema-request",
              {"schema_id": SID, "version": "0.1.0", "definition": defn, "x_kind": "record",
               "justification": "ADR-0109 S3: the constitution as a governed artifact inherited by every generation agent."}, cj=cj)
print("propose platform.constitution:", "OK" if not pe else pe)
dr, de = post(NET + "/v1/schema-requests/decide", {"schema_id": SID, "version": "0.1.0", "decision": "approve"}, tok=STOK)
print("approve platform.constitution:", (dr.get("published_schema") if dr else "ERR " + str(de)))

# --- 2. principles (structured, queryable index — faithful to constitution.yaml) ---
P = lambda i, st, mode, by, refs: {"id": i, "status": st, "enforce": {"mode": mode, "by": by}, "refs": refs}
principles = [
    P("GOVERNED-VOCABULARY",    "enforced",   "certify",       "conformance-suite + schema-naming-lint", ["ADR-0025", "ADR-0038", "ADR-0108"]),
    P("VERB-GRAMMAR",           "partial",    "lint",          "interface-house-rules (shadow)",          ["ADR-0108", "ADR-0097"]),
    P("ATTRIBUTED-WRITES",      "enforced",   "kernel",        "PROV_ENFORCE",                            ["ADR-0052"]),
    P("CERTIFIED-WRITER",       "partial",    "kernel",        "data-plane writer gate (shadow->enforce)", ["ADR-0052", "ADR-0051"]),
    P("BITEMPORAL-REGISTRY",    "enforced",   "kernel",        "registry",                                ["P1-registry"]),
    P("AUTH-VERIFIED-AT-KERNEL","enforced",   "kernel",        "services/authn",                          ["ADR-0068"]),
    P("HUMAN-DECIDES",          "enforced",   "pattern",       "decision spine",                          ["ADR-0029"]),
    P("PROMOTION-IS-REVIEWED",  "enforced",   "client+server", "aos.policy + PROD_PUBLISH_GATES",          ["ADR-0109"]),
    P("SANDBOX-PLANE-BOUNDARY", "enforced",   "sdk",           "aos BrokerBackend",                       ["ADR-0109"]),
    P("ACCESSIBLE-BY-DEFAULT",  "enforced",   "certify",       "a11y (axe) gate",                         ["ADR-0077"]),
    P("SOURCE-ATTRIBUTION",     "partial",    "certify+ui",    "attribution checks + display chrome",     ["ADR-0057", "ADR-0041"]),
    P("NO-SYNTHETIC-DATA",      "convention", "convention",    "review (engines report skips)",           ["project-rule"]),
    P("PRIVACY-BY-CONSENT",     "partial",    "kernel",        "per-object access.grant",                 ["ADR-0103"]),
    P("EVENT-ON-STATE-CHANGE",  "advisory",   "advisory",      "-",                                       ["ADR-0089"]),
]

yml = pathlib.Path(__file__).with_name("constitution.yaml").read_text()
rec = {"schema": SID, "id": CID, "title": "AirawatOS Constitution", "version": "0.1.0", "status": "active",
       "principles": principles, "source_yaml": yml}
_, we = post(KERNEL + "/v1/registry/objects", {"id": CID, "object": rec}, tok=STOK)
print("ingest constitution:", ("OK " + CID) if not we else ("ERR " + str(we)))

# --- 3. read-back + rollup ---
rr = post(KERNEL + "/v1/registry/resolve", {"id": CID}, tok=STOK)[0] or {}
rows = rr.get("results") or []
if rows:
    obj = rows[0].get("object") or rows[0]
    ps = obj.get("principles") or []
    roll = {s: sum(1 for p in ps if p.get("status") == s) for s in ("enforced", "partial", "advisory", "convention")}
    print(f"constitution live: {len(ps)} principles | {roll}")
    print("  advisory/convention (not yet mechanically blocked):",
          [p["id"] for p in ps if p.get("status") in ("advisory", "convention")])
else:
    print("read-back: not found", str(rr)[:150])
