"""Govern the public site-render verb as a network.interface contract (ADR-0058 / ADR-0111 L4).

`platform.render_site@0.1.0` — a request-modality verb the site-renderer engine implements: given a tenant +
path, return the rendered public artifact (HTML home page) built from that tenant's governed material. Governing
it makes certify's `governed-interfaces` check enforce it and lets the web adapter bind to the contract.
Steward-gated; idempotent. Run inside a service container that can reach network + identity.
"""
import json, os, urllib.request as u, urllib.error

NET = os.environ.get("NET", "http://airawat-network:8090")
IDN = os.environ.get("IDN", "http://airawat-identity:8093")


def login(tenant="airawat", user="steward"):
    r = u.Request(IDN + "/login", data=json.dumps({"username": user, "tenant": tenant}).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    d = json.loads(u.urlopen(r, timeout=15).read())
    return d.get("access_token") or d.get("token")


def post(url, body, tok):
    r = u.Request(url, data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok}, method="POST")
    try:
        return json.loads(u.urlopen(r, timeout=60).read() or b"{}"), None
    except urllib.error.HTTPError as e:
        return None, "%d:%s" % (e.code, e.read().decode()[:240])


DEF = {
    "modality": "request",
    "x-actor": "public",
    "description": "Render an agency's public web artifact for a path (default the home page) from that agency's "
                   "GOVERNED agency_profile + capability index + experience + publications — SEO head + schema.org "
                   "JSON-LD included. Read-only; it renders governed facts, it never writes. The web channel adapter "
                   "binds to this; a per-agency theme/renderer can swap it without host changes.",
    "request": {"type": "object", "required": ["tenant"], "properties": {
        "tenant": {"type": "string", "description": "the agency tenant whose governed public material is rendered"},
        "path": {"type": "string", "description": "which artifact to render; 'home' (default)"},
        "base": {"type": "string", "description": "deployment base URL for canonical/open links"},
        "locale": {"type": "string", "enum": ["en", "hi"], "description": "page language; default en"}}},
    "response": {"type": "object", "properties": {
        "content_type": {"type": "string", "description": "MIME type of the rendered body (e.g. text/html)"},
        "body": {"type": "string", "description": "the rendered artifact; empty if the tenant is not yet migrated"}}},
    "roles": [],
}


def main():
    tok = login()
    if not tok:
        raise SystemExit("no steward token")
    r, err = post(NET + "/v1/interfaces", {"interface_id": "platform.render_site", "version": "0.1.0", "definition": DEF}, tok)
    print("platform.render_site@0.1.0 ->", (err or r))


if __name__ == "__main__":
    main()
