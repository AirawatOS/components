"""Seed the hr.leave migration onto the generic Lifecycle plane (#263, ADR-0091 V3 / ADR-0097).

Writes governed DATA (no code): one `platform.lifecycle` declaration (per-tenant) + two `platform.verb_alias`
records (platform-global) that route the legacy hr verbs onto the generic lifecycle verbs, with retained-legacy
fallback. Non-breaking: where the declaration exists the generic path serves; elsewhere the alias falls back to
the bespoke hr-workflows engine.

Run INSIDE the app-host container (reaches identity/kernel by service name). Idempotent (stable ids).
    docker exec airawat-app-host python3 /app/components/engines/lifecycle-engine/seed_hr_leave.py [TENANT]
"""
import json, os, sys, urllib.request as u, urllib.error

IDN = os.environ.get("IDENTITY_URL", "http://identity:8093")
KRN = os.environ.get("KERNEL_API", "http://kernel:8080")
TENANT = sys.argv[1] if len(sys.argv) > 1 else "kmc"        # tenant to seed the declaration into
ADMIN_USER = sys.argv[2] if len(sys.argv) > 2 else f"{TENANT}-admin"   # a tenant principal (writes into the tenant are zero-trust gated)
HR_ENGINE = "ds:airawatdeveloper:engine/hr-workflows"


def post(url, body, hdr=None):
    r = u.Request(url, data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", **(hdr or {})}, method="POST")
    try:
        return json.loads(u.urlopen(r, timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code}:{e.read().decode()[:200]}"


def login(realm, user):
    r, e = post(f"{IDN}/realms/{realm}/login", {"username": user})
    if not r:
        r, e = post(f"{IDN}/login", {"username": user, "tenant": realm})
    return (r or {}).get("access_token"), (r or {}).get("tenant"), e


def kwrite(obj, tok):
    return post(f"{KRN}/v1/registry/objects", {"id": obj["id"], "object": obj}, {"Authorization": "Bearer " + tok})


# ---- 1) the hr.leave lifecycle declaration (per-tenant) -------------------------------------------
DECL = {
    "schema": "platform.lifecycle", "id": f"ds:{TENANT}:platform/lifecycle/hr-leave", "tenant": TENANT,
    "workflow": "hr.leave", "enabled": True, "record_schema": "hr.leave_request", "start": "draft",
    "states": ["draft", "pending", "approved", "rejected"],
    # PREPARE: resolve employee + manager (+login for routing) + org_unit scope, enforce leave-balance guard.
    "prepare": {"engine": HR_ENGINE, "verb": "leave-prepare"},
    "transitions": [
        {"name": "submit", "from": "draft", "to": "pending",
         "route": {"to_field": "manager_login", "title": "Leave request"}},
        {"name": "approve", "from": "pending", "to": "approved",
         "authorize": {"any": [{"actor_field": "manager_login"}]},
         "stamp": {"decided_by": "$sub", "decided_at": "$now"},
         "label": "Approve", "style": "pri"},
        {"name": "reject", "from": "pending", "to": "rejected",
         "authorize": {"any": [{"actor_field": "manager_login"}]},
         "stamp": {"decided_by": "$sub", "decided_at": "$now"},
         "label": "Reject", "style": "danger"},
    ],
}

# ---- 2) the verb aliases (platform-global; gated per-tenant by requires_workflow) -----------------
def alias(name, target, build, tenant):
    return {"schema": "platform.verb_alias", "id": f"ds:{tenant}:platform/verb_alias/{name}", "tenant": tenant,
            "alias": name, "enabled": True, "target_verb": target, "build": build,
            "requires_workflow": "hr.leave", "fallback_verb": name + "-legacy"}


def main():
    # steward → platform-global verb_alias; tenant-admin → the per-tenant declaration
    stok, stenant, se = login("airawat", "steward")
    if not stok:
        print("STEWARD LOGIN FAILED:", se); return
    atok, atenant, ae = login(TENANT, ADMIN_USER)
    if not atok:
        print(f"{TENANT} admin '{ADMIN_USER}' login failed ({ae}); trying steward for the declaration too"); atok = stok

    r, e = kwrite(DECL, atok)
    print(f"declaration hr.leave @ {TENANT}:", "ok" if r else e)

    aliases = [
        alias("leave-apply", "lifecycle-request",
              {"set": {"workflow": "hr.leave"}, "payload": ["leave_type", "days", "from_date", "to_date", "reason"]},
              stenant or "airawat"),
        alias("leave-decide", "lifecycle-decide",
              {"set": {"workflow": "hr.leave"}, "rename": {"leave_id": "case"},
               "map": {"field": "decision", "to": "transition", "cases": {"approve": "approve", "reject": "reject"}}},
              stenant or "airawat"),
    ]
    for a in aliases:
        r, e = kwrite(a, stok)
        print(f"verb_alias {a['alias']} → {a['target_verb']}:", "ok" if r else e)


if __name__ == "__main__":
    main()
