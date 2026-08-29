"""Grievance Handling solution (kind=solution) — the fulfilment DECLARATION for a civic grievance service.

Bound to a service offering (network.service_offering.solution). On a citizen submission the provider opens
a platform.case and routes the first task to the grievance officer's inbox. The reference app-host BPP enacts
this (ADR-0067 S4); a full ADR-0050 execution engine would interpret a multi-step workflow from `FULFILMENT`.
"""

FULFILMENT = {
    "on_submit": "open platform.case",
    "first_task": "Triage & assign the grievance",
    "assign_role": "civic.grievance-officer",
    "steps": ["received", "triaged", "in_progress", "resolved"],
}


def declaration():
    """Return this solution's fulfilment declaration (what a submission triggers)."""
    return FULFILMENT
