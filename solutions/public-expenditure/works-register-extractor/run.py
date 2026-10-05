"""Works Register Extractor — ingests the municipal Work Order register (already parsed from the Work
Order PDFs by Janaagraha's extractor) and writes GOVERNED public-works facts for the Public Expenditure
Dashboard. One extractor row → one works.work_order (+ its works.contract, works.contractor, works.payment).

I/O is ONLY the kernel broker: call(path, body) POSTs JSON to AIRAWAT_API+path with the run token.
Governed ids: ds:{tenant}:{noun-with-slashes}/{key}. Scaffolded from the service spec (ADR-0109); the works.*
nouns must be governed (from the spec's domain_model) before writes succeed.
"""
import os, json, urllib.request, hashlib

def call(path, body):
    rq = urllib.request.Request(os.environ["AIRAWAT_API"] + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": os.environ["RUN_TOKEN"]}, method="POST")
    return json.loads(urllib.request.urlopen(rq, timeout=30).read() or b"{}")

def _id(t, noun, key): return "ds:%s:%s/%s" % (t, noun.replace(".", "/"), key)
def _num(v):
    try: return float(str(v).replace(",", "")) if v not in (None, "", "NA") else None
    except Exception: return None
def _rows():
    # Wire to the real source (a bound input asset / filestore blob / a /v1/fetch of the extractor output).
    # Scaffold: read an optional INPUT_ROWS env (JSON list) so it runs end-to-end with a sample row.
    raw = os.environ.get("INPUT_ROWS"); return json.loads(raw) if raw else []

def main():
    T = call("/v1/context", {}).get("tenant")
    n_wo = n_pay = 0
    for r in _rows():
        wo_no = str(r.get("work_order_no") or r.get("job_number") or "").strip()
        if not wo_no: continue
        key = hashlib.sha1(wo_no.encode()).hexdigest()[:12]

        contractor_id = None
        cname = r.get("contractor_on_agreement") or r.get("contractor_name_road_history")
        if cname:
            contractor_id = _id(T, "works.contractor", hashlib.sha1(str(cname).encode()).hexdigest()[:12])
            call("/v1/write", {"scope": "works.contractor", "id": contractor_id, "object": {
                "schema": "works.contractor", "id": contractor_id, "tenant": T,
                "name": cname, "reg_no": r.get("contractor_reg_no")}})

        wo_id = _id(T, "works.work_order", key)
        call("/v1/write", {"scope": "works.work_order", "id": wo_id, "object": {
            "schema": "works.work_order", "id": wo_id, "tenant": T,
            "job_number": r.get("job_number"), "work_order_no": wo_no,
            "work_order_date": r.get("work_order_date"), "work_type": r.get("work_type"),
            "other_work": r.get("other_work"), "work_name": r.get("work_name"),
            "significant_landmark": r.get("significant_landmark"),
            "sanctioned_amount": _num(r.get("sanctioned_amount")), "sanctioned_year": r.get("sanctioned_year"),
            "extension_days": r.get("extension_days"), "ward": r.get("ward_no"), "zone": r.get("zone"),
            "completed_on": r.get("completed_on"), "final_cost": _num(r.get("final_cost")),
            "saving": _num(r.get("saving")), "completion_status": r.get("completion_verdict"),
            "confidence": _num(r.get("completion_confidence")),
            "prov": {"source": "bbmp_work_register", "completion_method": r.get("completion_method"),
                     "note": r.get("completion_note"), "excluded_pii": r.get("n_documents_excluded_pii")}}})
        n_wo += 1

        contract_id = _id(T, "works.contract", key)
        call("/v1/write", {"scope": "works.contract", "id": contract_id, "object": {
            "schema": "works.contract", "id": contract_id, "tenant": T, "work_order": wo_id,
            "contractor": contractor_id, "contract_value": _num(r.get("contract_value")),
            "discount_pct": _num(r.get("discount_pct")), "method": r.get("contract_method"),
            "agreement_date": r.get("agreement_date"),
            "dlp_from": r.get("dlp_from"), "dlp_to": r.get("dlp_to"), "dlp_status": r.get("dlp_status"),
            "confidence": _num(r.get("contract_confidence")), "prov": {"note": r.get("contract_confidence_note")}}})

        if r.get("paid_net") not in (None, ""):
            pay_id = _id(T, "works.payment", key)
            call("/v1/write", {"scope": "works.payment", "id": pay_id, "object": {
                "schema": "works.payment", "id": pay_id, "tenant": T, "work_order": wo_id,
                "contract": contract_id, "paid_net": _num(r.get("paid_net")),
                "paid_gross": _num(r.get("paid_gross")), "net_vs_contract": _num(r.get("net_vs_contract")),
                "payment_flag": r.get("payment_flag"), "paid_on": r.get("completed_on")}})
            n_pay += 1

    print("works-register-extractor: wrote %d work orders, %d payments" % (n_wo, n_pay))

if __name__ == "__main__":
    main()
