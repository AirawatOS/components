"""Seed the project.proposal migration onto the generic Lifecycle plane (#263 fan-out, ADR-0091 V3 / ADR-0097).

THIRD pattern — the CASE-SPINE (record_schema == platform.case): one case references MANY domain records
(project + versioned proposal) as subjects. Exercises: a start-transition {call} that WRITES the subjects
(proposal-prepare), a required approve {call} that commits the budget + writes the governance.decision
(proposal-commit), baseline supersede, and reject. Governed DATA only; non-breaking (requires_workflow +
proposal-submit-legacy fallback).

Note: only proposal-SUBMIT is aliased here. Approval runs through the generic Inbox task (lifecycle-decide on
the CASE id). The app's own decide-by-project-id button stays on the bespoke engine (that alias needs a
project→case lookup — a follow-on), so nothing breaks.

Run inside the app-host container. Idempotent.
    docker exec airawat-app-host python3 /app/components/engines/lifecycle-engine/seed_project_proposal.py [TENANT] [ADMIN]
"""
import json, os, sys, urllib.request as u, urllib.error

IDN = os.environ.get("IDENTITY_URL", "http://identity:8093")
KRN = os.environ.get("KERNEL_API", "http://kernel:8080")
TENANT = sys.argv[1] if len(sys.argv) > 1 else "nashik"
ADMIN = sys.argv[2] if len(sys.argv) > 2 else f"{TENANT}-admin"
PROJ = "ds:airawatdeveloper:engine/project-engine"


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
    "schema": "platform.lifecycle", "id": f"ds:{TENANT}:platform/lifecycle/project-proposal", "tenant": TENANT,
    "workflow": "project.proposal", "enabled": True, "record_schema": "platform.case", "start": "draft",
    "states": ["draft", "submitted", "approved", "rejected"],
    "transitions": [
        {"name": "submit", "from": "draft", "to": "submitted",
         # start-transition effect WRITES the two domain records; subjects: names in the result → case.subjects[]
         "effects": [{"call": {"engine": PROJ, "verb": "proposal-prepare", "args": {"data": "$payload"},
                               "subjects": ["project", "proposal"]}}],
         "route": {"role": "platform.tenant-admin", "title": "Project proposal — approval"}},
        {"name": "approve", "from": "submitted", "to": "approved",
         "authorize": {"any": [{"role": "platform.tenant-admin"}, {"actor_field": "sponsor"}]},
         "effects": [{"call": {"engine": PROJ, "verb": "proposal-commit", "required": True,
                               "args": {"case": "$case", "decision": "approve", "budget_code": "$payload.budget_code",
                                        "fiscal_year": "$payload.fiscal_year", "account": "$payload.account"}}}],
         "label": "Approve", "style": "pri"},   # spine mode: no stamp — platform.case is strict; the decided-by/at audit lives in the governance.decision + domain records
        {"name": "reject", "from": "submitted", "to": "rejected",
         "authorize": {"any": [{"role": "platform.tenant-admin"}, {"actor_field": "sponsor"}]},
         "effects": [{"call": {"engine": PROJ, "verb": "proposal-commit", "required": True,
                               "args": {"case": "$case", "decision": "reject", "rationale": "$payload.rationale"}}}],
         "stamp": {"decided_by": "$sub", "decided_at": "$now"}, "label": "Reject", "style": "danger"},
    ],
}


def alias(name, target, build, tenant, fallback):
    return {"schema": "platform.verb_alias", "id": f"ds:{tenant}:platform/verb_alias/{name}", "tenant": tenant,
            "alias": name, "enabled": True, "target_verb": target, "build": build,
            "requires_workflow": "project.proposal", "fallback_verb": fallback}


def main():
    stok, stenant, se = login("airawat", "steward")
    if not stok:
        print("STEWARD LOGIN FAILED:", se); return
    atok, atenant, ae = login(TENANT, ADMIN)
    if not atok:
        print(f"{TENANT} admin '{ADMIN}' login failed ({ae}); using steward"); atok = stok
    r, e = kwrite(DECL, atok)
    print(f"declaration project.proposal @ {TENANT}:", "ok" if r else e)
    a = alias("proposal-submit", "lifecycle-request",
              {"set": {"workflow": "project.proposal"},
               "payload": ["project", "proposal", "code", "budget_code", "fiscal_year", "account", "sponsor"]},
              stenant or "airawat", fallback="proposal-submit")   # absence-fallback → the bespoke handler (direct)
    r, e = kwrite(a, stok)
    print(f"verb_alias proposal-submit → lifecycle-request:", "ok" if r else e)
    # register a *-legacy fallback name → the bespoke handler (host ENGINE_VERBS may already have it; the seed
    # only writes the alias — the engine already exposes proposal-submit; -legacy resolves there via the host).
    print("note: approval runs via the generic Inbox task (lifecycle-decide on the case id).")


if __name__ == "__main__":
    main()
