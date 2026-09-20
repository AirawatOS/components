"""Seed the hr.expense migration onto the generic Lifecycle plane (#263 fan-out, ADR-0091 V3 / ADR-0097).

Second domain onto the generic engine — and the first that exercises a transition GUARD (approve-limit) + a
REQUIRED compute EFFECT (finance settlement). Governed DATA only: one platform.lifecycle declaration (per-tenant)
+ two platform.verb_alias records, both non-breaking (requires_workflow + *-legacy fallback).

Run inside the app-host container. Idempotent.
    docker exec airawat-app-host python3 /app/components/engines/lifecycle-engine/seed_hr_expense.py [TENANT] [ADMIN_USER]
"""
import json, os, sys, urllib.request as u, urllib.error

IDN = os.environ.get("IDENTITY_URL", "http://identity:8093")
KRN = os.environ.get("KERNEL_API", "http://kernel:8080")
TENANT = sys.argv[1] if len(sys.argv) > 1 else "nashik"
ADMIN_USER = sys.argv[2] if len(sys.argv) > 2 else f"{TENANT}-admin"
HR = "ds:airawatdeveloper:engine/hr-workflows"
FIN = "ds:airawatdeveloper:engine/finance-engine"


def post(url, body, hdr=None):
    r = u.Request(url, data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", **(hdr or {})}, method="POST")
    try:
        return json.loads(u.urlopen(r, timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code}:{e.read().decode()[:200]}"


def login(realm, user):
    r, e = post(f"{IDN}/login", {"username": user, "tenant": realm})
    if not r:
        r, e = post(f"{IDN}/realms/{realm}/login", {"username": user})
    return (r or {}).get("access_token"), (r or {}).get("tenant"), e


def kwrite(obj, tok):
    return post(f"{KRN}/v1/registry/objects", {"id": obj["id"], "object": obj}, {"Authorization": "Bearer " + tok})


DECL = {
    "schema": "platform.lifecycle", "id": f"ds:{TENANT}:platform/lifecycle/hr-expense", "tenant": TENANT,
    "workflow": "hr.expense", "enabled": True, "record_schema": "hr.expense_claim", "start": "draft",
    "states": ["draft", "pending", "approved", "rejected"],
    "prepare": {"engine": HR, "verb": "expense-prepare"},
    "transitions": [
        {"name": "submit", "from": "draft", "to": "pending",
         "route": {"to_field": "manager_login", "title": "Expense claim"}},
        {"name": "approve", "from": "pending", "to": "approved",
         "authorize": {"any": [{"actor_field": "manager_login"}]},
         "guard": {"compute": {"engine": HR, "verb": "expense-check"}},   # budget present + approval-limit
         "effects": [{"call": {"engine": FIN, "verb": "finance-post", "required": True,
                               "link_field": "posted", "into": "finance_txn",
                               "args": {"direction": "expenditure", "amount": "$record.amount",
                                        "budget": "$payload.budget", "contra_type": "liability",
                                        "status": "committed", "source": "$case", "source_kind": "hr.expense_claim",
                                        "project": "$record.project", "org_unit": "$scope.org_unit",
                                        "memo": "$record.description"}}}],
         "stamp": {"decided_by": "$sub", "decided_at": "$now", "budget": "$payload.budget"},
         "label": "Approve", "style": "pri"},
        {"name": "reject", "from": "pending", "to": "rejected",
         "authorize": {"any": [{"actor_field": "manager_login"}]},
         "stamp": {"decided_by": "$sub", "decided_at": "$now"}, "label": "Reject", "style": "danger"},
    ],
}


def alias(name, target, build, tenant):
    return {"schema": "platform.verb_alias", "id": f"ds:{tenant}:platform/verb_alias/{name}", "tenant": tenant,
            "alias": name, "enabled": True, "target_verb": target, "build": build,
            "requires_workflow": "hr.expense", "fallback_verb": name + "-legacy"}


def main():
    stok, stenant, se = login("airawat", "steward")
    if not stok:
        print("STEWARD LOGIN FAILED:", se); return
    atok, atenant, ae = login(TENANT, ADMIN_USER)
    if not atok:
        print(f"{TENANT} admin '{ADMIN_USER}' login failed ({ae}); using steward"); atok = stok
    r, e = kwrite(DECL, atok)
    print(f"declaration hr.expense @ {TENANT}:", "ok" if r else e)
    aliases = [
        alias("expense-apply", "lifecycle-request",
              {"set": {"workflow": "hr.expense"},
               "payload": ["category", "amount", "currency", "description", "incurred_on", "budget", "project"]},
              stenant or "airawat"),
        alias("expense-decide", "lifecycle-decide",
              {"set": {"workflow": "hr.expense"}, "rename": {"expense_id": "case"}, "payload": ["budget"],
               "map": {"field": "decision", "to": "transition", "cases": {"approve": "approve", "reject": "reject"}}},
              stenant or "airawat"),
    ]
    for a in aliases:
        r, e = kwrite(a, stok)
        print(f"verb_alias {a['alias']} → {a['target_verb']}:", "ok" if r else e)


if __name__ == "__main__":
    main()
