"""Evolve platform.agency_profile 0.1.0 -> 0.2.0 (ADR-0111): make the public-presence ARCHETYPE first-class.

Additive / backward-compatible — every new field is optional + nullable, so existing profiles stay valid:
  archetype    the PRIMARY participant archetype — selects theme + chrome + schema.org @type in the site-renderer
  archetypes   all hats a participant wears (e.g. an NGO that also publishes components); primary first
  accent_color per-participant accent (the site-renderer themes the masthead rule / card hover from it)
  emblem       a short emoji/seal shown when no logo_url is set

Until now the site-renderer could only INFER the archetype from the legacy `agency_type` string; governing these
fields lets a participant declare it explicitly (and wear several hats). Run inside app-host (reaches net+kernel+idn).
"""
import json, os, urllib.request as u, urllib.error

NET = os.environ.get("NET", "http://airawat-network:8090")
KERNEL = os.environ.get("KERNEL", "http://airawat-kernel:8080")
IDN = os.environ.get("IDN", "http://airawat-identity:8093")
ARCHETYPES = ["government", "developer", "operator", "provider", "aggregator", "ngo", "network"]


def login(user, tenant):
    r = u.Request(IDN + "/login", data=json.dumps({"username": user, "tenant": tenant}).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(u.urlopen(r, timeout=15).read())["access_token"]


def post(url, body, tok):
    r = u.Request(url, data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok}, method="POST")
    try:
        return json.loads(u.urlopen(r, timeout=30).read() or b"{}"), None
    except urllib.error.HTTPError as e:
        return None, "%d %s" % (e.code, e.read().decode()[:260])


NS = lambda: {"type": ["string", "null"]}
NAR = lambda it="string": {"type": ["array", "null"], "items": ({"type": it} if isinstance(it, str) else it)}
ID = {"type": "string", "pattern": "^ds:[a-z0-9-]+:\\S+$"}
SOC = {"type": "object", "properties": {"platform": {"type": "string"}, "url": {"type": "string"}}, "additionalProperties": True}
ARCH = {"type": ["string", "null"], "enum": ARCHETYPES + [None],
        "description": "primary public-presence archetype; selects the site theme/chrome. Default inferred from agency_type."}
defn = {"type": "object", "title": "platform.agency_profile", "x-kind": "entity",
        "required": ["schema", "id", "display_name"],
        "properties": {"id": ID, "schema": {"const": "platform.agency_profile"}, "tenant": NS(),
            "display_name": {"type": "string"}, "legal_name": NS(), "agency_type": NS(), "jurisdiction": NS(),
            "parent_authority": NS(), "mandate": NS(), "about": NS(), "logo_url": NS(), "seal_url": NS(),
            "emblem": NS(), "primary_color": NS(), "accent_color": NS(), "website": NS(),
            "archetype": ARCH, "archetypes": {"type": ["array", "null"], "items": {"type": "string", "enum": ARCHETYPES}},
            "email": NS(), "phone": NS(), "address": NS(), "languages": NAR("string"), "social": NAR(SOC),
            "established": NS(), "prov": {"type": ["object", "null"]}},
        "additionalProperties": False,
        "description": "An agency's institutional identity for its public presence (ADR-0111). Generic — any "
                       "participant archetype (government / developer / operator / provider / aggregator / ngo / network)."}


def main():
    stok = login("steward", "airawat")
    print("govern platform.agency_profile@0.2.0:",
          post(NET + "/v1/schemas", {"schema_id": "platform.agency_profile", "version": "0.2.0",
                                     "definition": defn, "sensitivity": "public"}, stok))


if __name__ == "__main__":
    main()
