"""Govern the fund-flow-engine ADVANCE + FLOW verbs as network.interface contracts (ADR-0058).

Sibling to funding-engine/govern_interfaces.py. Promotes the public-finance lifecycle verbs from app-host BFF
routing labels into signed governed verbs so certify's `governed-interfaces` check enforces them.

Run-dispatch name (kebab, app_host ENGINE_VERBS)  →  governed verb id (dot, <domain>.<subject>)
    flow-stamp            → finance.flow_stamp
    flow-status          → finance.flow_status
    advance-disburse     → finance.advance_disburse
    advance-liquidate    → finance.advance_liquidate
    advance-reconcile    → finance.advance_reconcile
    advance-outstanding  → finance.advance_outstanding

`finance` is already a governed domain. hrt-summary (the HRT watchtower read) is intentionally left ungoverned —
it is a report assembly, not an advance/flow action. Publishes as the airawat standards body (steward-gated),
idempotent.
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


N_ADV = "finance.advance"; N_FF = "finance.fund_flow"; N_TXN = "finance.transaction"
N_ACCT = "finance.account"; N_PROJECT = "project.project"
STAGES = ["approved", "allocated", "released", "committed", "paid", "expended"]


def obj(required, props):
    d = {"type": "object", "properties": props}
    if required:
        d["required"] = required
    return d


INTERFACES = {
 "finance.flow_stamp": {
   "modality": "request", "x-actor": "finance-officer",
   "description": "Stamp an amount at a stage of a project's fund-flow ladder "
                  "(approved→allocated→released→committed→paid→expended) and recompute the bottleneck (the first "
                  "stage that has fallen below the upstream peak). Call to record money moving down the ladder.",
   "request": obj(["project", "stage", "amount"], {
       "project": {"type": "string", "x-ref": N_PROJECT}, "stage": {"type": "string", "enum": STAGES},
       "amount": {"type": "number", "description": "₹cr at this stage"},
       "channel": {"type": "string", "enum": ["government", "grant", "partner"], "default": "government"},
       "add": {"type": "boolean", "description": "add to the existing stage figure vs set it"}}),
   "response": obj(["fund_flow"], {
       "fund_flow": {"type": "string", "x-ref": N_FF}, "current_stage": {"type": "string"},
       "bottleneck": {"type": "string"}, "stages": {"type": "object"}})},

 "finance.flow_status": {
   "modality": "request", "x-actor": "finance-officer|analyst",
   "description": "Read-only: the fund-flow ladder (stage amounts + current stage + bottleneck) for a project / "
                  "channel, or for every flow in the tenant.",
   "request": obj([], {
       "project": {"type": "string", "x-ref": N_PROJECT},
       "channel": {"type": "string", "enum": ["government", "grant", "partner"]}}),
   "response": obj(["flows", "count"], {
       "flows": {"type": "array", "items": {"type": "object"}, "x-ref": N_FF}, "count": {"type": "integer"}})},

 "finance.advance_disburse": {
   "modality": "request", "x-actor": "finance-officer",
   "description": "Disburse an advance to a spending unit: Dr Advances-Receivable / Cr Cash — money out as a "
                  "tracked RECEIVABLE, not an expense. Writes finance.advance (outstanding = amount) via the "
                  "double-entry core and stamps the project's fund_flow 'released'.",
   "request": obj(["amount"], {
       "amount": {"type": "number", "description": "₹cr, positive"}, "to": {"type": "string", "description": "recipient spending unit"},
       "project": {"type": "string", "x-ref": N_PROJECT},
       "channel": {"type": "string", "enum": ["government", "grant", "partner"]},
       "advance_account": {"type": "string", "x-ref": N_ACCT, "description": "Advances-Receivable (asset)"},
       "cash_account": {"type": "string", "x-ref": N_ACCT, "description": "Cash/Bank paid from (asset)"},
       "purpose": {"type": "string"}, "program": {"type": "string"}, "budget": {"type": "string"}, "source": {"type": "string"}}),
   "response": obj(["advance", "outstanding"], {
       "advance": {"type": "string", "x-ref": N_ADV}, "amount": {"type": "number"},
       "outstanding": {"type": "number"}, "status": {"type": "string"}, "transaction": {"type": "string", "x-ref": N_TXN}})},

 "finance.advance_liquidate": {
   "modality": "request", "x-actor": "finance-officer",
   "description": "Liquidate an advance against proof: Dr Expense / Cr Advances — recognizes the expenditure and "
                  "clears part/all of the receivable. Reduces outstanding; status → partially_liquidated | "
                  "liquidated; stamps the project's fund_flow 'expended'. Defaults amount to the full outstanding.",
   "request": obj(["advance"], {
       "advance": {"type": "string", "x-ref": N_ADV}, "amount": {"type": "number", "description": "defaults to outstanding"},
       "expense_account": {"type": "string", "x-ref": N_ACCT, "description": "expense account to charge"},
       "proof_ref": {"type": "string", "description": "utilization-certificate / proof ref"},
       "budget": {"type": "string", "description": "optional budget control at recognition"}}),
   "response": obj(["advance", "outstanding", "status"], {
       "advance": {"type": "string", "x-ref": N_ADV}, "liquidated_now": {"type": "number"},
       "total_liquidated": {"type": "number"}, "outstanding": {"type": "number"}, "status": {"type": "string"},
       "transaction": {"type": "string", "x-ref": N_TXN}})},

 "finance.advance_reconcile": {
   "modality": "request", "x-actor": "finance-officer",
   "description": "Close a fully-liquidated advance (outstanding ≈ 0) → status reconciled. Refuses if any "
                  "outstanding balance remains.",
   "request": obj(["advance"], {"advance": {"type": "string", "x-ref": N_ADV}}),
   "response": obj(["advance", "status"], {
       "advance": {"type": "string", "x-ref": N_ADV}, "status": {"type": "string"}})},

 "finance.advance_outstanding": {
   "modality": "request", "x-actor": "finance-officer|analyst",
   "description": "Read-only watch-list: unliquidated advances (outstanding > 0) with age, filtered by "
                  "project / program / recipient — the outstanding/unliquidated-balance indicator.",
   "request": obj([], {
       "project": {"type": "string", "x-ref": N_PROJECT}, "program": {"type": "string"},
       "to_party": {"type": "string", "description": "recipient spending unit"}}),
   "response": obj(["outstanding_total", "advances"], {
       "outstanding_total": {"type": "number"}, "count": {"type": "integer"},
       "advances": {"type": "array", "items": {"type": "object"}, "x-ref": N_ADV}})},
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
            ok += 1; print(f"  ✓ {iid}@{VER}")
        else:
            print(f"  ✗ {iid}: {err}")
    print(f"published {ok}/{len(INTERFACES)} fund-flow interfaces")
    cat = json.loads(u.urlopen(f"{NET}/v1/interfaces", timeout=20).read())["interfaces"]
    fin = sorted(i["interface_id"] for i in cat if i["interface_id"].startswith("finance."))
    print("finance.* verbs now in catalog:", fin)
    print("catalog total:", len(cat))


if __name__ == "__main__":
    main()
