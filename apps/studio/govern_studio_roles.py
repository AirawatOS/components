#!/usr/bin/env python3
"""ADR-0113 Studio v2 — S0: govern the 5 `studio.*` project roles (the collaboration actor vocabulary).

A role must live in a GOVERNED domain and is SIGNED into existence by the standards body (the `airawat`
steward) — a developer cannot self-add one (components may only DECLARE roles that already resolve).
This mirrors the M1 water-schema governance: steward bare-login → network POSTs. Idempotent + re-runnable.

Run inside the app-host container (which can reach identity:8093 + network:8090), like water_m1.py:
    docker exec airawat-app-host python3 /tmp/govern_studio_roles.py
"""
import json, urllib.request, urllib.error

IDP = "http://identity:8093"
NET = "http://network:8090"

ROLES = [
    ("studio.product-manager",  "Product Manager",  "Owns the solution's outcomes, backlog and acceptance criteria; signs off releases."),
    ("studio.project-manager",  "Project Manager",  "Owns assignments, milestones and merge gates; invites members, assigns components, approves change-requests."),
    ("studio.business-analyst", "Business Analyst",  "Owns the data contract — schemas, interfaces, taxonomy/config — that unblocks parallel build."),
    ("studio.developer",        "Developer",         "Builds the component(s) they own or are granted; opens change-requests."),
    ("studio.test-engineer",    "Test Engineer",     "Owns conformance + acceptance suites; gates certification and integration."),
]


def post(url, body, tok=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST"), timeout=20)
        return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read()[:200].decode(errors="ignore")}


def get(url):
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20).read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read()[:200].decode(errors="ignore")}


def main():
    st = post(IDP + "/login", {"username": "steward", "tenant": "airawat"}).get("access_token")
    assert st, "steward login failed (bare-login identity:8093/login)"

    # 1) the `studio` domain must be governed before roles in it (role lint requires a governed domain).
    d = post(NET + "/v1/domains", {"domain": "studio", "label": "Solution Studio"}, tok=st)
    print("domain studio:", d.get("published") or d)

    # 2) sign the 5 project roles.
    for role, label, defn in ROLES:
        r = post(NET + "/v1/roles", {"role": role, "label": label, "definition": defn}, tok=st)
        print(f"  role {role}:", r.get("published") or r)

    # 3) PROVE S0 acceptance — the 5 roles resolve in the governed catalogue.
    listed = {x["role"] for x in get(NET + "/v1/roles").get("roles", [])}
    want = {r[0] for r in ROLES}
    ok = want <= listed
    print(f"\nS0 check — all 5 studio.* roles resolve in governance: {'✓ PASS' if ok else '✗ FAIL'}")
    if not ok:
        print("  missing:", sorted(want - listed))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
