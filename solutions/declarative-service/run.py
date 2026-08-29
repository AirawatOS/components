"""Declarative Service Runner (kind=solution) — the GENERIC fulfiller for config-driven civic services.

A service's intake FORM (request_schema) and WORKFLOW (fulfillment.workflow: an ordered list of
{step, role, task}) live as CONFIG on the service offering, authored by a tenant admin in Service Studio.
This solution is the certified trust anchor; the reference interpreter enacts the config: open a case on
submit, route each step's task to the declared governed role's inbox, and advance on completion. No bespoke
code per service — complex services bind their own certified solution instead.
"""


def fulfil(offering, submission):
    """Reference contract: given the offering (with request_schema + fulfillment.workflow) and a citizen
    submission, the platform opens a case and drives the declared workflow. The interpreter lives in the
    app-host BPP today; a full ADR-0050 engine would run this per-instance."""
    return {"opens": "platform.case", "then": "run fulfillment.workflow step-by-step to role inboxes"}
