"""aos.policy — the dev-vs-production governance boundary (P2).

Policy (set 2026-09-26):
  • A developer CAN build, govern, install and test a whole solution IN THEIR OWN
    (development) tenant — schema/role/interface governance is auto-approved there.
  • A developer CANNOT publish anything into shared production surfaces — the
    Marketplace (store) or the Service Directory (service offerings).
  • Anything running in a development tenant is clearly marked "Under Development"
    to end users.

This module is the single place that decides those things, so the SDK, the CLI, and
(eventually) the Studio UI all enforce the same rule. The server-side gates it maps to
are named in PROD_PUBLISH_GATES for the network team.
"""

DEV = "development"
PROD = "production"

# Shared production surfaces a dev artifact must NOT reach (enforced here + server-side).
PROTECTED_TARGETS = {
    "marketplace": "POST /v1/store/publish",
    "service-directory": "POST /v1/service-offerings",
}

# Where the network must also enforce this (defence in depth — the SDK gate is convenience,
# these are the authority): refuse a publish whose source tenant / actor is a development tenant.
PROD_PUBLISH_GATES = [
    "network /v1/store/publish  → reject if publisher tenant environment == development",
    "network /v1/service-offerings → reject if tenant environment == development",
    "app_host marketplace/service bridges → never forward a development-tenant publish",
]


def is_dev(environment):
    return (environment or DEV) == DEV


def can_govern_in_place(environment):
    """Dev tenants self-approve their own schemas/roles/interfaces. Prod routes to the
    standards body (request → decide)."""
    return is_dev(environment)


def check_publish(target, environment):
    """Return (allowed: bool, reason: str). Called by `aos publish`."""
    if target not in PROTECTED_TARGETS:
        return True, f"'{target}' is not a protected production surface"
    if is_dev(environment):
        return (False,
                f"BLOCKED: a development-tenant artifact cannot be published to the "
                f"{target} ({PROTECTED_TARGETS[target]}). Build & test it in your sandbox; "
                f"promotion to production is a separate, standards-body-gated step.")
    return True, f"production publish to {target} permitted"


def dev_marking(meta):
    """The 'Under Development' chrome an app/offering/solution carries in a dev tenant.
    Surfaces (Studio shelf, City Scan, Jan Seva, the Service Directory listing) render this."""
    if not is_dev(meta.get("environment")):
        return None
    return {
        "environment": DEV,
        "status": "under_development",
        "badge": {"label": "Under Development", "tone": "warning",
                  "note": "Built in a developer sandbox — not a published production service."},
    }


def stamp_dev(meta, environment=DEV):
    """Stamp a component/solution manifest with its environment + dev marking, so the
    marking travels with the artifact (install, listing, render) rather than being bolted on."""
    m = dict(meta)
    m["environment"] = environment
    mk = dev_marking(m)
    if mk:
        m["status"] = mk["status"]
        m["ui_badge"] = mk["badge"]
    return m
