"""Govern the federated funding VERBS as network.interface contracts (ADR-0058 / ADR-0072).

The funding-engine's cross-participant verbs are, until now, app-host BFF routing labels governed only at the
DATA layer (install-grants against governed funding.*/finance.* schemas). This promotes each to a signed
`network.interface` in the standards catalog — the same governance justice.file_case has — so certify's
`governed-interfaces` check enforces them and the actor·verb·noun graph carries the funding federation edges.

Run-dispatch name (kebab, app_host ENGINE_VERBS)  →  governed verb id (dot, <domain>.<subject> snake)
    funding-publish-program   → funding.publish_program
    funding-award             → funding.award
    funding-stack             → funding.stack
    funding-release-tranche   → funding.release_tranche
    funding-record-receipt    → funding.record_receipt
    funding-received          → funding.received
    funding-subscribe         → funding.subscribe
    funding-revoke-subscription → funding.revoke_subscription
    funding-milestone         → funding.milestone
    funding-monitor           → funding.monitor
    funding-commit-revenue    → funding.commit_revenue

Publishes directly as the airawat standards body (POST /v1/interfaces, steward-gated). Idempotent: re-publishing
the same id@version re-signs it. `funding` is already a governed domain (its nouns are governed).
"""
import json, os, urllib.request as u, urllib.error

NET = os.environ.get("NET_URL", "http://network:8090")
IDN = os.environ.get("IDN_URL", "http://identity:8093")
VER = "0.1.0"


def post(url, body, hdr=None):
    r = u.Request(url, data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", **(hdr or {})}, method="POST")
    try:
        return json.loads(u.urlopen(r, timeout=60).read()), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code}:{e.read().decode()[:240]}"


# ---- governed-noun refs (all already published schemas) -------------------------------------------
N_PROGRAM = "funding.program"; N_AWARD = "funding.award"; N_GRANT = "funding.progress_grant"
N_PROPOSAL = "project.proposal"; N_PROJECT = "project.project"; N_OUTCOME = "project.outcome"
N_TXN = "finance.transaction"; N_BUDGET = "finance.budget"; N_VC = "credential.milestone"


def obj(required, props):
    d = {"type": "object", "properties": props}
    if required:
        d["required"] = required
    return d


# ---- the 11 funding verbs -------------------------------------------------------------------------
INTERFACES = {
 "funding.publish_program": {
   "modality": "request", "x-actor": "funder",
   "description": "A funder agency publishes a funding programme / scheme with a sized envelope. Call once per "
                  "scheme, before any awards are made against it.",
   "request": obj(["funder", "title", "envelope_cr"], {
       "funder": {"type": "string", "description": "funder agency ref"},
       "title": {"type": "string"}, "scheme": {"type": "string"},
       "envelope_cr": {"type": "number", "description": "total programme envelope, ₹cr"},
       "currency": {"type": "string", "default": "INR"},
       "co_funders": {"type": "array", "items": {"type": "string"}},
       "description": {"type": "string"}, "eligibility": {"type": "object"}}),
   "response": obj(["program"], {
       "program": {"type": "string", "x-ref": N_PROGRAM, "description": "the published programme id"},
       "funder": {"type": "string"}, "envelope_cr": {"type": "number"},
       "co_funders": {"type": "array", "items": {"type": "string"}}})},

 "funding.award": {
   "modality": "request", "x-actor": "funder",
   "description": "A funder sanctions a share of a proposal's ask as a signed commitment. Co-funders stack onto "
                  "the same proposal; returns the running stack (committed vs ask) after this award.",
   "request": obj(["program", "proposal", "funder", "amount_cr"], {
       "program": {"type": "string", "x-ref": N_PROGRAM},
       "proposal": {"type": "string", "x-ref": N_PROPOSAL, "description": "the implementer's proposal (read cross-tenant)"},
       "funder": {"type": "string"}, "amount_cr": {"type": "number"},
       "share": {"type": "number", "description": "fractional share of the ask, optional"},
       "project": {"type": "string", "x-ref": N_PROJECT}, "decision": {"type": "string"},
       "currency": {"type": "string", "default": "INR"}}),
   "response": obj(["award"], {
       "award": {"type": "string", "x-ref": N_AWARD}, "funder": {"type": "string"},
       "amount_cr": {"type": "number"}, "ask_cr": {"type": "number"}, "committed_cr": {"type": "number"},
       "gap_cr": {"type": "number"}, "funded": {"type": "boolean"}, "awards": {"type": "integer"}})},

 "funding.stack": {
   "modality": "request", "x-actor": "funder|implementer",
   "description": "Read-only: aggregate all awards on a proposal ACROSS funder tenants (aggregator lane) against "
                  "its ask — the co-funding stack. Use to check whether a proposal is fully funded.",
   "request": obj(["proposal"], {
       "proposal": {"type": "string", "x-ref": N_PROPOSAL},
       "funders": {"type": "array", "items": {"type": "string"}, "description": "funder tenant slugs to include"}}),
   "response": obj(["ask_cr", "committed_cr", "gap_cr", "funded"], {
       "proposal": {"type": "string"}, "ask_cr": {"type": "number"}, "committed_cr": {"type": "number"},
       "gap_cr": {"type": "number"}, "funded": {"type": "boolean"}, "awards": {"type": "integer"},
       "breakdown": {"type": "array", "items": {"type": "object"}}})},

 "funding.release_tranche": {
   "modality": "request", "x-actor": "funder",
   "description": "THE GATE. A funder releases a tranche against an award — but ONLY when (1) an active progress "
                  "subscription authorises it to read the project AND (2) a milestone credential verifies against "
                  "the implementer's key. Posts the disbursement as expenditure in the funder's own ledger. "
                  "Information gates money: no verified milestone → the release BLOCKS.",
   "request": obj(["award", "amount_cr"], {
       "award": {"type": "string", "x-ref": N_AWARD}, "amount_cr": {"type": "number"},
       "expense_account": {"type": "string", "description": "funder-side expense account to charge"}}),
   "response": obj(["gate"], {
       "gate": {"type": "string", "enum": ["OPEN", "BLOCKED"]}, "released": {"type": "number"},
       "award": {"type": "string", "x-ref": N_AWARD}, "funder": {"type": "string"},
       "verified_by": {"type": "string", "description": "the milestone/outcome that opened the gate"},
       "transaction": {"type": "string", "x-ref": N_TXN}, "award_released_cr": {"type": "number"},
       "award_status": {"type": "string"}})},

 "funding.record_receipt": {
   "modality": "request", "x-actor": "implementer",
   "description": "Run by the implementer: book a funder's released tranche as REVENUE in its own ledger, "
                  "reconciled against the funder's signed award (read cross-tenant). Closes the money loop — "
                  "two sets of books, one shared award.",
   "request": obj(["award"], {
       "award": {"type": "string", "x-ref": N_AWARD},
       "revenue_account": {"type": "string", "description": "implementer-side revenue account"}}),
   "response": obj(["received", "total_received_cr"], {
       "received": {"type": "number", "description": "newly booked this call"},
       "total_received_cr": {"type": "number"}, "funder": {"type": "string"},
       "transaction": {"type": "string", "x-ref": N_TXN}, "project": {"type": "string", "x-ref": N_PROJECT}})},

 "funding.received": {
   "modality": "request", "x-actor": "implementer|funder",
   "description": "Read-only: total funding received on a project, broken down by source award.",
   "request": obj(["project"], {"project": {"type": "string", "x-ref": N_PROJECT}}),
   "response": obj(["received_cr"], {
       "project": {"type": "string"}, "received_cr": {"type": "number"}, "receipts": {"type": "integer"},
       "by_source": {"type": "array", "items": {"type": "object"}}})},

 "funding.subscribe": {
   "modality": "request", "x-actor": "implementer",
   "description": "Run by the implementer: grant a funder a STANDING, REVOCABLE read of one project's progress. "
                  "This subscription (not a blanket data grant) is the precondition for tranche release.",
   "request": obj(["subscriber", "project"], {
       "subscriber": {"type": "string", "description": "funder tenant/agency to authorise"},
       "project": {"type": "string", "x-ref": N_PROJECT},
       "scope": {"type": "array", "items": {"type": "string"}, "description": "readable schemas"}}),
   "response": obj(["subscription", "status"], {
       "subscription": {"type": "string", "x-ref": N_GRANT}, "subscriber": {"type": "string"},
       "project": {"type": "string"}, "status": {"type": "string"}})},

 "funding.revoke_subscription": {
   "modality": "request", "x-actor": "implementer",
   "description": "Run by the implementer: revoke a progress subscription. Future tranche releases to that funder "
                  "then BLOCK at the gate — withdrawing consent withdraws the money path.",
   "request": obj(["subscription"], {"subscription": {"type": "string", "x-ref": N_GRANT}}),
   "response": obj(["revoked", "status"], {
       "revoked": {"type": "string", "x-ref": N_GRANT}, "status": {"type": "string"}})},

 "funding.milestone": {
   "modality": "request", "x-actor": "implementer",
   "description": "Run by the implementer: issue a signed milestone credential (ADR-0049 VC) to each funder holder "
                  "— the verifiable proof of progress that opens the release gate. Verified by the issuer's key, "
                  "holder-agnostic.",
   "request": obj(["project"], {
       "project": {"type": "string", "x-ref": N_PROJECT}, "milestone": {"type": "string", "default": "m1"},
       "holders": {"type": "array", "items": {"type": "string"}, "description": "funder holders to issue to"}}),
   "response": obj(["issued"], {
       "project": {"type": "string"}, "milestone": {"type": "string"},
       "issued": {"type": "array", "items": {"type": "object"}, "x-ref": N_VC}})},

 "funding.monitor": {
   "modality": "request", "x-actor": "funder",
   "description": "A funder's monitoring view: for each project it funds, its progress subscription (read "
                  "cross-tenant from the implementer) plus the milestone credentials it holds, verified.",
   "request": obj([], {"funder": {"type": "string", "description": "defaults to the caller's agency"}}),
   "response": obj(["projects"], {
       "funder": {"type": "string"}, "projects": {"type": "array", "items": {"type": "object"}}})},

 "funding.commit_revenue": {
   "modality": "request", "x-actor": "implementer",
   "description": "Run by the implementer: roll funder AWARDS into the project budget as COMMITTED revenue and "
                  "receipts as REALIZED revenue, so the spend plan shows it is backed by funder commitments.",
   "request": obj(["project"], {
       "project": {"type": "string", "x-ref": N_PROJECT},
       "funders": {"type": "array", "items": {"type": "string"}},
       "reconcile_allocated": {"type": "boolean", "default": True}}),
   "response": obj(["budget"], {
       "budget": {"type": "string", "x-ref": N_BUDGET}, "committed_revenue": {"type": "number"},
       "realized_revenue": {"type": "number"}, "allocated": {"type": "number"},
       "reconciled_to_ask": {"type": "number"}, "awards": {"type": "integer"}, "funded": {"type": "boolean"}})},
}


def main():
    r, e = post(f"{IDN}/realms/airawat/login", {"username": "steward"})
    if not r:
        print("LOGIN FAILED:", e); return
    STOK = "Bearer " + r["access_token"]
    ok = 0
    for iid, defn in INTERFACES.items():
        res, err = post(f"{NET}/v1/interfaces", {"interface_id": iid, "version": VER, "definition": defn}, {"Authorization": STOK})
        if res:
            ok += 1; print(f"  ✓ {iid}@{VER}  ({res['published']['modality']})")
        else:
            print(f"  ✗ {iid}: {err}")
    print(f"published {ok}/{len(INTERFACES)} funding interfaces")
    cat = json.loads(u.urlopen(f"{NET}/v1/interfaces", timeout=20).read())["interfaces"]
    fund = sorted(i["interface_id"] for i in cat if i["interface_id"].startswith("funding."))
    print("funding.* now in catalog:", fund)
    print("catalog total:", len(cat))


if __name__ == "__main__":
    main()
