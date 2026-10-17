#!/usr/bin/env python3
"""
Worker Career Passport — a live, end-to-end demo of ADR-0112 / ADR-0049 / ADR-0067.

A gig/unorganised worker self-registers ONCE, gets a portable network identity (`sub`), and then every
authority they touch ISSUES a signed Verifiable Credential into their wallet — keyed to that one `sub`.
The credentials compound into a verifiable *career passport* that the worker carries across platforms:

    skill  ->  get a job  ->  get paid  ->  get protected  ->  get a better job  ->  (loop)

Issuers (each a real network tenant with its own Vault signing key):
    uidai   eKYC / identity assurance      -> credential.identity_verified
    iti     Industrial Training Institute   -> credential.skill_certificate
    labour  Ministry of Labour (eShram)     -> credential.eshram_worker
    ola     gig platform (employer)         -> credential.work_experience (90-day), proof_of_work, proof_of_payment
    swiggy  gig platform (employer)         -> credential.work_experience (general)   [portability]
    esic    worker insurance                -> credential.insurance_enrolled
    ncs     National Career Service         -> credential.job_placement               [the "better job"]

Everything is REAL: real governed schemas, real tenants with Vault steward keys, real Ed25519-signed VCs,
real signature verification. The worker is the integration point — no central database is merged.

Run INSIDE the app-host container (reaches network:8090 + identity:8093):
    docker cp run.py airawat-app-host:/tmp/run.py && docker exec airawat-app-host python3 /tmp/run.py
"""
import json, os, time, urllib.request, urllib.error

NET = os.environ.get("NETWORK_URL", "http://network:8090")
IDP = os.environ.get("IDENTITY_URL", "http://identity:8093")

# ---------- tiny HTTP helpers ----------
# POST /users on the reference IdP is guarded by an internal provisioner token (the network uses it on
# join-approve). Supplied at runtime via the IDENTITY_INTERNAL env var — never hardcoded in this file.
IDENTITY_INTERNAL = os.environ.get("IDENTITY_INTERNAL", "")

def _req(method, url, body=None, tok=None, internal=False):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    if internal and IDENTITY_INTERNAL:
        h["X-Internal-Token"] = IDENTITY_INTERNAL
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        return urllib.request.urlopen(r, timeout=20).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {e.read()[:300].decode(errors='ignore')}")

def post(url, body, tok=None, internal=False): return json.loads(_req("POST", url, body, tok, internal) or b"{}")
def get(url):                  return json.loads(_req("GET", url) or b"{}")

def section(t): print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)
def ok(t):      print(f"  ✓ {t}")

# ---------- 0. standards-body (airawat steward) token: governs schemas + creates tenants ----------
section("0. Standards body authenticates (airawat steward)")
steward = post(IDP + "/login", {"username": "steward", "tenant": "airawat"})["access_token"]
ok("airawat steward token minted (role platform.steward)")

# ---------- 1. Govern the 8 credential types (ADR-0049 — type must be a governed credential.* schema) ----------
section("1. Govern the credential catalogue (network standards body)")
CRED_SCHEMAS = {
    "credential.identity_verified": ["full_name", "id_ref", "verified_on", "method"],
    "credential.skill_certificate": ["trade", "nsqf_level", "grade", "institute", "issued_on"],
    "credential.eshram_worker":     ["uan", "occupation", "category", "registered_on"],
    "credential.work_experience":   ["employer", "role", "from", "to", "days", "kind"],
    "credential.proof_of_work":     ["employer", "period", "nature_of_work"],
    "credential.proof_of_payment":  ["employer", "amount_inr", "period", "mode"],
    "credential.insurance_enrolled":["scheme", "cover_inr", "enrolled_on", "policy_no"],
    "credential.job_placement":     ["employer", "role", "wage_band", "placed_on", "channel"],
}
for sid, props in CRED_SCHEMAS.items():
    defn = {"type": "object",
            "properties": {p: {"type": ("integer" if p in ("days", "cover_inr", "amount_inr") else "string")} for p in props},
            "required": props[:2]}
    post(NET + "/v1/schemas", {"schema_id": sid, "version": "0.1.0", "definition": defn, "sensitivity": "internal"})
    ok(f"governed {sid}")

# ---------- 1b. Govern the `identity` domain + the UIDAI Aadhaar ENROLMENT record schema ----------
# The prerequisite layer: UIDAI enrols a worker (identity.aadhaar) BEFORE any Avsar account; eKYC verifies it.
section("1b. Govern identity domain + identity.aadhaar (UIDAI enrolment record)")
try:
    post(NET + "/v1/domains", {"domain": "identity", "label": "Identity & eKYC"}, tok=steward)
    ok("governed domain 'identity'")
except RuntimeError as e:
    ok("domain 'identity' (already governed)" if "already" in str(e) or "422" in str(e) else "domain identity: " + str(e)[:60])
post(NET + "/v1/schemas", {"schema_id": "identity.aadhaar", "version": "0.1.0", "sensitivity": "sensitive",
     "definition": {"type": "object", "properties": {p: {"type": "string"} for p in
                    ["uid", "name", "dob", "gender", "address", "mobile", "status"]}, "required": ["uid", "name"]}})
ok("governed identity.aadhaar (UIDAI enrolment / CIDR record)")

# ---------- 2. Create the 7 issuer tenants (each auto-gets a Vault steward signing key) ----------
section("2. Register the issuer tenants (each gets a Vault signing key)")
TENANTS = {
    "uidai":  "UIDAI eKYC Authority",
    "iti":    "Industrial Training Institute",
    "labour": "Ministry of Labour & Employment (eShram)",
    "ola":    "Ola (gig platform / employer)",
    "swiggy": "Swiggy (gig platform / employer)",
    "esic":   "Employees' State Insurance (worker cover)",
    "ncs":    "National Career Service",
}
for tid, name in TENANTS.items():
    r = post(NET + "/v1/tenants", {"tenant_id": tid, "name": name, "environment": "production",
                                   "jurisdiction": "IN"}, tok=steward)
    ok(f"{tid:7s} -> steward {r['registered']['steward_participant']}")

# ---------- 3. Provision one admin login per tenant (so each can issue as itself) ----------
section("3. Provision an issuer login per tenant")
ADMINS = {}  # tenant -> token
for tid in TENANTS:
    uname = f"{tid}-admin"
    try:
        post(IDP + "/users", {"username": uname, "tenant": tid, "roles": ["platform.tenant-admin"]}, internal=True)
    except RuntimeError as e:
        if "409" not in str(e):
            raise
    ADMINS[tid] = post(IDP + f"/realms/{tid}/login", {"username": uname})["access_token"]
    ok(f"{uname} provisioned + logged in")

# ---------- 3b. Register the Ola embedding app as a GOVERNED platform.app_client (ADR-0112 S1b) ----------
# Replaces the old hardcoded "ola-embed" default. redirect_uris = the gig app's OWN origin (a different
# origin from airos.airawat.org) → registering it is what AUTHORISES that origin for cross-origin /embed/*
# fetches (app-host derives its CORS allow-list from these records). Written to the kernel registry (airawat
# tenant) with the steward token.
section("3b. Register the Ola embedding app (governed platform.app_client)")
KERNEL = os.environ.get("KERNEL_URL", "http://kernel:8080")
OLA_REDIRECTS = ["http://localhost:8777/ola.html", "http://127.0.0.1:8777/ola.html"]
_ola_rec = {"schema": "platform.app_client", "id": "ds:airawat:app_client/ola", "client_id": "ola",
            "name": "Ola Partner", "participant": "ola", "redirect_uris": OLA_REDIRECTS,
            "scopes": ["profile", "service:invoke"], "status": "active", "brand": "Avsar",  # hosted page: "Sign in with Avsar"
            "service_domains": ["welfare", "employment"],  # relevance filter: show only worker services, not municipal
            "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
post(KERNEL + "/v1/registry/objects", {"id": _ola_rec["id"], "object": _ola_rec}, tok=steward)
ok("platform.app_client 'ola' registered → redirect_uris: " + ", ".join(OLA_REDIRECTS))
ok("→ CORS now authorises origins http://localhost:8777 + http://127.0.0.1:8777 for /embed/*")

# ---------- 4. The WORKER opens their AVSAR account (one portable livelihood identity) ----------
# "Avsar" (अवसर, Hindi for "opportunity") = the worker's livelihood account + wallet. It is the single
# portable identity every credential below is keyed to — the face of the worker-as-integration-point.
section("4. Worker opens their AVSAR account (Register into Avsar — network citizenship)")
WORKER = "ravi.kumar"
try:
    res = post(IDP + "/register", {"username": WORKER, "name": "Ravi Kumar", "contact": "ravi@example.in"})
    ok("new Avsar account created")
except RuntimeError as e:
    if "409" not in str(e):
        raise
    res = post(IDP + f"/realms/citizens/login", {"username": WORKER})
    ok("Avsar account already existed — logged in")
worker_tok = res["access_token"]
SUB = res["claims"]["sub"]
print(f"  -> Avsar livelihood ID (sub): {SUB}")

# ---------- 5. Issue the career passport, in arc order ----------
def issue(issuer, ctype, title, claims):
    r = post(NET + "/v1/credentials/issue",
             {"issuer_tenant": issuer, "holder": SUB, "type": ctype, "title": title, "claims": claims},
             tok=ADMINS[issuer])
    c = r["credential"]
    ok(f"{issuer:7s} issued {title}  [{c['id'].rsplit('/',1)[-1]}]  key={c['signature']['key_id']}")
    return c

def verify(cid, who):
    v = post(NET + "/v1/credentials/verify", {"credential_id": cid})
    flag = "VALID" if v.get("verified") else f"INVALID ({v.get('reason')})"
    ok(f"{who} verifies {cid.rsplit('/',1)[-1]}  -> signature_valid={v.get('signature_valid')} "
       f"holder=={('yes' if v.get('holder')==SUB else 'NO')}  [{flag}]")
    return v

TODAY = time.strftime("%Y-%m-%d", time.gmtime())
section("5. The career arc — each authority issues a signed credential to the worker's sub")

print("\n-- GET SKILLED (identity + skilling + registration) --")
issue("uidai",  "credential.identity_verified", "Verified Identity (eKYC)",
      {"full_name": "Ravi Kumar", "id_ref": "xxxx-xxxx-1234", "verified_on": TODAY, "method": "aadhaar-otp"})
skill = issue("iti", "credential.skill_certificate", "Electrician — NSQF Level 4",
              {"trade": "Electrician", "nsqf_level": "4", "grade": "A", "institute": "ITI Kanpur", "issued_on": TODAY})
issue("labour", "credential.eshram_worker", "eShram Worker (UAN)",
      {"uan": "UAN-33-0099-7788", "occupation": "Electrician", "category": "gig", "registered_on": TODAY})

print("\n-- GET A JOB + GET PAID (Ola hires; gig work becomes verifiable history) --")
exp90 = issue("ola", "credential.work_experience", "Work Experience (90-day) — Ola",
              {"employer": "Ola", "role": "Delivery/Install Partner", "from": "2026-07-01", "to": "2026-09-29",
               "days": 90, "kind": "ninety_day"})
pow_c = issue("ola", "credential.proof_of_work", "Proof of Work — Ola",
              {"employer": "Ola", "period": "Jul-Sep 2026", "nature_of_work": "Last-mile electrical installs"})
issue("ola", "credential.proof_of_payment", "Proof of Payment — Ola",
      {"employer": "Ola", "amount_inr": 54000, "period": "Jul-Sep 2026", "mode": "bank-transfer"})

print("\n-- GET PROTECTED (ESIC verifies worker + work, then enrols) --")
verify(skill["id"], "esic")          # a relying agency checks the worker's proofs ...
verify(pow_c["id"], "esic")
issue("esic", "credential.insurance_enrolled", "Accident Cover (ESIC)",
      {"scheme": "PMSBY-gig", "cover_inr": 200000, "enrolled_on": TODAY, "policy_no": "ESIC-GIG-55123"})

print("\n-- PORTABILITY (same wallet, a second platform) --")
issue("swiggy", "credential.work_experience", "Work Experience (general) — Swiggy",
      {"employer": "Swiggy", "role": "Delivery Partner", "from": "2026-10-01", "to": TODAY,
       "days": 20, "kind": "general"})

print("\n-- GET A BETTER JOB (NCS verifies skill + experience, then places) --")
verify(skill["id"], "ncs")
verify(exp90["id"], "ncs")
issue("ncs", "credential.job_placement", "Placed — Facility Electrician",
      {"employer": "Acme Facilities Pvt Ltd", "role": "Facility Electrician", "wage_band": "18-22k",
       "placed_on": TODAY, "channel": "NCS match"})

# ---------- 6. Publish benefit/job SERVICE offerings (so an embedding app can DISCOVER them) ----------
section("6. Publish services into the federation (discoverable from any embedding app)")
def offer(tenant, sid, title, domain, issues, requires, ctitle, cclaims):
    post(NET + "/v1/service-offerings",
         {"tenant": tenant, "service_id": sid, "domain": domain, "title": title,
          "interaction": ["search", "init", "route"], "audience": "civic.resident",
          "required_credentials": requires,
          "fulfillment": {"issues_credential": issues, "credential_title": ctitle, "credential_claims": cclaims}},
         tok=ADMINS[tenant])
    ok(f"{tenant:7s} published service '{title}'")
offer("esic", "gig-accident-cover", "Gig Worker Accident Cover", "welfare",
      "credential.insurance_enrolled", ["credential.identity_verified", "credential.eshram_worker"],
      "Accident Cover (ESIC)", ["scheme", "cover_inr", "enrolled_on", "policy_no"])
offer("ncs", "better-job-match", "Better Job Match", "employment",
      "credential.job_placement", ["credential.skill_certificate", "credential.work_experience"],
      "Job Placement", ["employer", "role", "wage_band", "placed_on", "channel"])

# ---------- 7. The passport: everything the worker now holds, from one sub ----------
section("7. The AVSAR career passport (one wallet, keyed to the sub)")
wallet = get(NET + "/v1/credentials?holder=" + urllib.parse.quote(SUB))["credentials"]
print(f"  Avsar account = {SUB}\n  {len(wallet)} verifiable credentials held in the Avsar wallet:\n")
print(f"  {'ISSUER':8s} {'CREDENTIAL':28s} {'SIG':5s} {'TITLE'}")
print("  " + "-" * 74)
for c in wallet:
    v = post(NET + "/v1/credentials/verify", {"credential_id": c["id"]})
    print(f"  {c['issuer_tenant']:8s} {c['type'].replace('credential.',''):28s} "
          f"{'ok' if v.get('signature_valid') else 'BAD':5s} {c.get('title','')}")
print("\n  Every row above is an independently-signed, holder-bound, portable proof.")
print("  With ONE Avsar account the worker proved skill -> got a job -> got paid -> got protected -> got a")
print("  better job, carrying one wallet across UIDAI, ITI, eShram, Ola, Swiggy, ESIC and NCS. No database merged.")
section("DONE")
