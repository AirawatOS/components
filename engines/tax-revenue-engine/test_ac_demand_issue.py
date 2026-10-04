"""Conformance test for the Property Tax service_spec acceptance criteria realized by tax-revenue-engine
(ADR-0109 S4b). These are the executable GIVEN/WHEN/THEN that turn the spec's `test: null` green.

Pure-stdlib; runs anywhere:  python3 test_ac_demand_issue.py
"""
import run


class FakeCtx:
    """Minimal ctx double — captures writes so the test can assert what the engine produced."""
    tenant = "airawatdeveloper"
    def __init__(self): self.written = {}
    def now(self): return "2026-04-01T00:00:00Z"
    def write(self, i, o): self.written[i] = o
    def get(self, i): return self.written.get(i)


def test_PT_ASSESS_FORMULA():
    # amount = assessed_value × base_rate × use_factor × (1 − exemption)
    assert run.assess_amount(1_000_000, base_rate=0.0015, use_factor=1.0, exemption=0.0) == 1500.0
    assert run.assess_amount(1_000_000, base_rate=0.0015, use_factor=1.2, exemption=0.1) == 1620.0


def test_PT_DEMAND_DUEDATE():
    # due_date = issue_date + grace_days
    assert run.due_date("2026-04-01", 30) == "2026-05-01"
    assert run.due_date("2026-12-20", 30) == "2027-01-19"


def test_AC_DEMAND_ISSUE():
    """GIVEN a verified assessment WHEN raise-demand runs THEN a tax.demand is issued with a computed
    amount AND a due_date (the spec's AC-DEMAND-ISSUE)."""
    ctx = FakeCtx()
    d = run.h_raise_demand(ctx, {
        "parcel_id": "P-123", "assessment": "ds:airawatdeveloper:tax.assessment/A1",
        "period": "2026-27", "assessed_value": 500_000, "issue_date": "2026-04-01"})
    assert d["schema"] == "tax.demand"
    assert d["status"] == "issued"
    assert d["amount"] == 750.0          # 500000 × 0.0015
    assert d["due_date"] == "2026-05-01"  # 2026-04-01 + 30d
    assert d["id"] in ctx.written         # it was actually written
    return d


def test_AC_COLLECT():
    """GIVEN an issued demand WHEN a full payment is recorded THEN the demand advances to 'paid'."""
    ctx = FakeCtx()
    d = run.h_raise_demand(ctx, {"parcel_id": "P-9", "period": "2026-27", "assessed_value": 200_000, "issue_date": "2026-04-01"})
    run.h_record_payment(ctx, {"demand": d["id"], "amount": d["amount"], "channel": "upi", "receipt_no": "RC1"})
    assert ctx.get(d["id"])["status"] == "paid"


if __name__ == "__main__":
    test_PT_ASSESS_FORMULA()
    test_PT_DEMAND_DUEDATE()
    d = test_AC_DEMAND_ISSUE()
    test_AC_COLLECT()
    print(f"AC-DEMAND-ISSUE: PASS — {d['id']} amount={d['amount']} due={d['due_date']} status={d['status']}")
    print("ALL PASS — AC-DEMAND-ISSUE, AC-COLLECT, PT-ASSESS-FORMULA, PT-DEMAND-DUEDATE")
