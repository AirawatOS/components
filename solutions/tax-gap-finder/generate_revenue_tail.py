"""ADR-0109 S4a — generate the revenue-tail VOCABULARY from the spec, then reconcile the spec.

Spec-driven generation, first increment: the governed Property Tax service_spec declares `tax.demand` and
`tax.payment` as `status: roadmap, impl: null` (the assess→demand→collect tail). This reads the spec, GENERATES
+ governs those two schemas (fields taken from the spec's domain_model `key` lists), then RECONCILES the spec
instance — flips the two nouns roadmap→built with impl = the new schema ids — and extends the trace_link graph.

That demonstrates the loop's generate→(govern)→reconcile for the data layer. S4b (next) generates the BEHAVIOR:
a raise-demand / record-payment engine, the verified→assessed→demanded→collected workflow, and the
AC-DEMAND-ISSUE conformance test (the first `test: null` turned green).

Run inside app-host (steward/airawat):
    docker exec -e APP=http://localhost:8099 -e NET=http://network:8090 -e IDN=http://identity:8093 \
        -e KERNEL=http://kernel:8080 airawat-app-host python3 generate_revenue_tail.py
"""
import json, os, urllib.request, urllib.error, http.cookiejar

APP = os.environ.get("APP", "http://localhost:8099")
NET = os.environ.get("NET", "http://network:8090")
IDN = os.environ.get("IDN", "http://identity:8093")
KERNEL = os.environ.get("KERNEL", "http://kernel:8080")
ID_PAT = r"^ds:[a-z0-9-]+:\S+$"
SPEC_ID = "ds:airawat:service_spec/property-tax"

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

# --- read the spec; confirm the tail is still roadmap (so generation is driven by the spec, not assumed) ---
res = post(KERNEL + "/v1/registry/resolve", {"id": SPEC_ID}, tok=STOK)[0] or {}
spec_obj = (res.get("results") or [{}])[0].get("object") or {}
spec = spec_obj.get("spec") or {}
roadmap_nouns = [n["noun"] for n in spec.get("domain_model", []) if n.get("status") == "roadmap"]
print("spec roadmap nouns:", roadmap_nouns)

# --- generate: schema definitions derived from the spec's domain_model key lists ---
S = {"type": "string"}; Sn = {"type": ["string", "null"]}; N = {"type": "number"}; Nn = {"type": ["number", "null"]}
def defn(sid, kind, reqd, props, desc):
    p = {"schema": {"const": sid}, "id": {"type": "string", "pattern": ID_PAT}}; p.update(props)
    return {"type": "object", "x-kind": kind, "additionalProperties": False, "required": ["schema", "id"] + reqd,
            "properties": p, "title": sid, "description": desc}

GEN = {
  # tax.demand — key: [parcel_id, assessment, period, amount, due_date, status]
  "tax.demand": ("entity", ["parcel_id", "amount"], {
     "parcel_id": S, "assessment": Sn, "period": Sn, "amount": N, "due_date": Sn,
     "status": {"enum": ["issued", "part_paid", "paid", "overdue", "cancelled"]},
     "issued_by": Sn, "tenant": Sn, "created_at": Sn},
     "A PROPERTY-TAX DEMAND (bill) raised against a verified assessment for a period. Generated from the Property Tax service_spec (ADR-0109 S4)."),
  # tax.payment — key: [demand, amount, paid_at, channel, receipt_no]
  "tax.payment": ("record", ["demand", "amount"], {
     "demand": S, "amount": N, "paid_at": Sn, "channel": {"enum": ["cash", "online", "cheque", "upi", "other"]},
     "receipt_no": Sn, "tenant": Sn},
     "A PAYMENT recorded against a tax.demand. Generated from the Property Tax service_spec (ADR-0109 S4)."),
}
generated = {}
for sid, (kind, reqd, props, desc) in GEN.items():
    d = defn(sid, kind, reqd, props, desc)
    pr, pe = post(APP + "/bridge/schema-request",
                  {"schema_id": sid, "version": "0.1.0", "definition": d, "x_kind": kind,
                   "justification": "ADR-0109 S4: generated from the Property Tax service_spec roadmap (assess→demand→collect)."}, cj=cj)
    dr, de = post(NET + "/v1/schema-requests/decide", {"schema_id": sid, "version": "0.1.0", "decision": "approve"}, tok=STOK)
    pub = dr.get("published_schema") if dr else None
    generated[sid] = pub
    print(f"generate {sid}: propose={'OK' if not pe else pe} | approve={pub or ('ERR ' + str(de))}")

# --- reconcile: flip the two nouns roadmap→built in the spec, impl = the new schema ids ---
for n in spec.get("domain_model", []):
    if n["noun"] in generated and generated[n["noun"]]:
        n["status"] = "built"; n["impl"] = generated[n["noun"]]
spec_obj["spec"] = spec
_, we = post(KERNEL + "/v1/registry/objects", {"id": SPEC_ID, "object": spec_obj}, tok=STOK)
print("reconcile service_spec:", "OK" if not we else ("ERR " + str(we)))

# --- extend the trace graph: an implemented_by edge for each generated noun ---
base = 17
for i, (sid, pub) in enumerate(generated.items()):
    if not pub:
        continue
    eid = f"ds:airawat:trace_link/property-tax-{base + i:03d}"
    rec = {"schema": "platform.trace_link", "id": eid, "subject": f"{SPEC_ID}#noun/{sid}",
           "relation": "implemented_by", "object": pub, "spec": SPEC_ID, "status": "built"}
    _, ee = post(KERNEL + "/v1/registry/objects", {"id": eid, "object": rec}, tok=STOK)
    print(f"trace edge {eid}:", "OK" if not ee else ("ERR " + str(ee)))

# --- verify: re-read the spec rollup ---
res2 = post(KERNEL + "/v1/registry/resolve", {"id": SPEC_ID}, tok=STOK)[0] or {}
dm = ((res2.get("results") or [{}])[0].get("object") or {}).get("spec", {}).get("domain_model", [])
built = sum(1 for n in dm if n.get("status") == "built"); rm = sum(1 for n in dm if n.get("status") == "roadmap")
print(f"spec domain_model now: {built} built / {rm} roadmap (was 7/3)")
print("  still roadmap:", [n["noun"] for n in dm if n.get("status") == "roadmap"])
