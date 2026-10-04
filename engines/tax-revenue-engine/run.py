"""tax-revenue-engine — the assess → demand → collect tail of Property Tax.

GENERATED from the Property Tax service_spec (ADR-0109 S4): it realizes the roadmap operations
`tax.raise_demand` / `tax.record_payment`, the rules PT-ASSESS-FORMULA + PT-DEMAND-DUEDATE, and the
`verified → assessed → demanded → collected` workflow, writing the generated `tax.demand` / `tax.payment`
nouns. Pure compute is factored out (assess_amount / due_date) so the conformance test can check it directly.
"""
from datetime import date, timedelta

# PT-ASSESS-FORMULA / PT-DEMAND-DUEDATE defaults (a real deployment overrides via tax.match_policy).
DEFAULTS = {"base_rate": 0.0015, "use_factor": 1.0, "exemption": 0.0, "grace_days": 30}


def assess_amount(assessed_value, base_rate=None, use_factor=None, exemption=None):
    """PT-ASSESS-FORMULA: annual_tax = assessed_value × base_rate × use_factor × (1 − exemption)."""
    br = DEFAULTS["base_rate"] if base_rate is None else base_rate
    uf = DEFAULTS["use_factor"] if use_factor is None else use_factor
    ex = DEFAULTS["exemption"] if exemption is None else exemption
    return round(float(assessed_value) * br * uf * (1.0 - ex), 2)


def due_date(issue_date, grace_days=None):
    """PT-DEMAND-DUEDATE: due_date = issue_date + grace_days (ISO date in, ISO date out)."""
    g = DEFAULTS["grace_days"] if grace_days is None else grace_days
    y, m, d = map(int, issue_date.split("-"))
    return (date(y, m, d) + timedelta(days=int(g))).isoformat()


def h_raise_demand(ctx, body):
    """verified → demanded: compute the demand from the verified assessment and issue a tax.demand."""
    av = body.get("assessed_value")
    if av is None:
        raise ValueError("assessed_value required (from the verified assessment)")
    amount = assess_amount(av, body.get("base_rate"), body.get("use_factor"), body.get("exemption"))
    issue = body.get("issue_date") or ctx.now()[:10]
    period = body.get("period")
    did = f"ds:{ctx.tenant}:tax/demand/{body['parcel_id']}-{period}"
    rec = {"schema": "tax.demand", "id": did, "parcel_id": body["parcel_id"],
           "assessment": body.get("assessment"), "period": period, "amount": amount,
           "due_date": due_date(issue, body.get("grace_days")), "status": "issued",
           "issued_by": body.get("issued_by"), "tenant": ctx.tenant, "created_at": ctx.now()}
    ctx.write(did, rec)
    return rec


def h_record_payment(ctx, body):
    """demanded → collected: record a payment and advance the demand (paid / part_paid)."""
    demand = ctx.get(body["demand"])
    amt = float(body["amount"])
    pid = f"ds:{ctx.tenant}:tax/payment/{body['demand'].split('/')[-1]}-{body.get('receipt_no', 'r')}"
    pay = {"schema": "tax.payment", "id": pid, "demand": body["demand"], "amount": amt,
           "paid_at": body.get("paid_at") or ctx.now(), "channel": body.get("channel", "other"),
           "receipt_no": body.get("receipt_no"), "tenant": ctx.tenant}
    ctx.write(pid, pay)
    if demand:
        demand["status"] = "paid" if amt >= float(demand.get("amount", 0)) else "part_paid"
        ctx.write(demand["id"], demand)
    return pay


HANDLERS = {"raise-demand": h_raise_demand, "record-payment": h_record_payment}
