"""Seed the campaign assign→verify migration onto the generic Lifecycle plane (#263 fan-out, ADR-0091/0097).

Fourth shape: a single-record lifecycle on campaign.assignment where the transitions have CROSS-RECORD effects
(update the candidate) and the decide computes ground truth + a governance.decision. Exercises the generic
engine's opener_field (no hardcoded applicant_login), {update} effect, required compute effect, and multi-field
`link` (copy several compute results onto the record). campaign-create (Data plane) + campaign-progress (Compute
plane) are NOT lifecycle — they migrate later. Governed DATA only; non-breaking.

Run inside the app-host container. Idempotent.
    docker exec airawat-app-host python3 /app/components/engines/lifecycle-engine/seed_campaign.py [TENANT] [ADMIN]
"""
import json, os, sys, urllib.request as u, urllib.error

IDN = os.environ.get("IDENTITY_URL", "http://identity:8093")
KRN = os.environ.get("KERNEL_API", "http://kernel:8080")
TENANT = sys.argv[1] if len(sys.argv) > 1 else "nashik"
ADMIN = sys.argv[2] if len(sys.argv) > 2 else f"{TENANT}-admin"
CAMP = "ds:airawatdeveloper:engine/campaign-engine"


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


# NOTE: the domain (tax) lives ENTIRELY in this declaration's config (outcome vocabulary, status mapping, captured
# fields, revenue on approve) — ADR-0100 C2. The campaign-engine handlers are domain-neutral. A different campaign
# type = a different declaration with different config, zero engine change (see DECL_INSPECTION below).
DECL = {
    "schema": "platform.lifecycle", "id": f"ds:{TENANT}:platform/lifecycle/campaign-verify", "tenant": TENANT,
    "workflow": "campaign.verify", "enabled": True, "record_schema": "campaign.assignment", "start": "new",
    "states": ["new", "assigned", "done"],
    "prepare": {"engine": CAMP, "verb": "assign-prepare", "args": {"copy_fields": ["parcel_id"]}},
    "transitions": [
        {"name": "assign", "from": "new", "to": "assigned",
         "effects": [{"update": {"ref": "$payload.candidate", "patch": {"status": "assigned"}}}],
         "route": {"to_field": "assignee", "title": "Field verify"}},
        {"name": "verify", "from": "assigned", "to": "done",
         "authorize": {"any": [{"actor_field": "assignee"}]},
         "effects": [{"call": {"engine": CAMP, "verb": "verify-decide", "required": True,
                               "args": {"case": "$case", "outcome": "$payload.outcome", "status_field": "status",
                                        "outcome_status": {"confirmed": "verified", "not_taxable": "dismissed", "not_found": "dismissed"},
                                        "approve_outcomes": ["confirmed"],
                                        "ground_truth": {"built_area": "$payload.built_area", "assessed_value": "$payload.assessed_value",
                                                         "occupancy": "$payload.occupancy", "notes": "$payload.notes"},
                                        "on_approve_set": {"estimated_revenue": "$payload.assessed_value"}},
                               "link": {"decision": "decision", "ground_truth": "ground_truth", "outcome": "outcome"}}}],
         "stamp": {"decided_at": "$now"}, "label": "Record verification", "style": "pri"},
    ],
}


def alias(name, target, build, tenant, fallback):
    return {"schema": "platform.verb_alias", "id": f"ds:{tenant}:platform/verb_alias/{name}", "tenant": tenant,
            "alias": name, "enabled": True, "target_verb": target, "build": build,
            "requires_workflow": "campaign.verify", "fallback_verb": fallback}


def main():
    stok, stenant, se = login("airawat", "steward")
    if not stok:
        print("STEWARD LOGIN FAILED:", se); return
    atok, atenant, ae = login(TENANT, ADMIN)
    if not atok:
        print(f"{TENANT} admin '{ADMIN}' login failed ({ae}); using steward"); atok = stok
    r, e = kwrite(DECL, atok)
    print(f"declaration campaign.verify @ {TENANT}:", "ok" if r else e)
    for a in [
        alias("campaign-assign", "lifecycle-request",
              {"set": {"workflow": "campaign.verify"}, "payload": ["candidate", "officer", "campaign"]},
              stenant or "airawat", fallback="campaign-assign"),
        alias("verify-submit", "lifecycle-decide",
              {"set": {"workflow": "campaign.verify", "transition": "verify"}, "rename": {"assignment": "case"},
               "payload": ["outcome", "built_area", "assessed_value", "occupancy", "notes"]},
              stenant or "airawat", fallback="verify-submit"),
    ]:
        r, e = kwrite(a, stok)
        print(f"verb_alias {a['alias']} → {a['target_verb']}:", "ok" if r else e)


if __name__ == "__main__":
    main()
