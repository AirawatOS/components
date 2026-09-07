"""Court Case Management solution (kind=solution) — the case_lifecycle DECLARATION.

The Court Console app enacts this: each transition is an attributed, governed write (component://
court-console obo the signed-in officer). A full ADR-0050 execution engine would interpret the same
`FULFILMENT` graph server-side. States mirror justice.case + justice.filing + justice.hearing.
"""

FULFILMENT = {
    "on_submit": "open platform.case",
    "workflow": "justice.case_lifecycle",
    "entry": {"type": "user-initiated", "form": "File a case", "role": "justice.advocate"},
    "steps": [
        {"step": "filed",       "role": "justice.advocate",         "writes": ["justice.filing", "justice.case"]},
        {"step": "scrutiny",    "role": "justice.scrutiny-officer", "writes": ["justice.scrutiny_result"],
         "decision": "accept | return-for-correction (loops to filed)"},
        {"step": "registration","role": "justice.registrar",        "writes": ["justice.case (case_number)"]},
        {"step": "hearing",     "role": "justice.bench-clerk",      "writes": ["justice.hearing", "justice.appearance"],
         "decision": "hold | adjourn (creates next hearing)"},
        {"step": "disposed",    "role": "justice.judge",            "writes": ["justice.disposal", "justice.case (disposed)"],
         "terminal": True},
    ],
    "reuses": {
        "pack": "ds:airawat:pack/justice",
        "spine": ["platform.case", "platform.task", "platform.inbox_item"],
    },
}


def declaration():
    return FULFILMENT
