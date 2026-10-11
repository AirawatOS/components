"""Seed public-presence profiles + capability indexes for the network's archetypes (ADR-0111), grounded in REAL
content discovered on the live network — no synthetic data:

  airawat          network   — the governed catalog + participant directory (182 certified components, 22 participants)
  airawatdeveloper developer — real published components (site-renderer, converse-engine, telegram-concierge, agents)
  kmc              government— Kanpur municipal services + civic data
  nashik           government— 8 LIVE service offerings (Birth Certificate, Property Tax, …) render automatically
  new-delhi        government— citizen services portal

Also evolves platform.capability 0.1.0 -> 0.2.0 (additive): the `type` enum gains the component-economy kinds
(component/solution/model/runtime/agent) and `target_kind` gains `component`, so a developer/operator/network
presence can list software as first-class capabilities. Run inside app-host (reaches net+kernel+idn). Idempotent.
"""
import json, os, urllib.request as u, urllib.error

KERNEL = os.environ.get("KERNEL", "http://airawat-kernel:8080")
NET = os.environ.get("NET", "http://airawat-network:8090")
IDN = os.environ.get("IDN", "http://airawat-identity:8093")


def login(tenant, user="steward"):
    r = u.Request(IDN + "/login", data=json.dumps({"username": user, "tenant": tenant}).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(u.urlopen(r, timeout=15).read())["access_token"]


def post(url, body, tok):
    r = u.Request(url, data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok}, method="POST")
    try:
        return "ok", json.loads(u.urlopen(r, timeout=30).read() or b"{}")
    except urllib.error.HTTPError as e:
        return "ERR%d" % e.code, e.read().decode()[:160]


def put(tenant, obj, tok):
    return post(KERNEL + "/v1/registry/objects", {"id": obj["id"], "object": obj}, tok)


# ---- 1) evolve platform.capability -> 0.2.0 (additive enum extension) ----
def evolve_capability_schema():
    S = lambda t="string": {"type": t}
    NS = lambda: {"type": ["string", "null"]}
    NB = lambda: {"type": ["boolean", "null"]}
    NO = lambda: {"type": ["object", "null"]}
    NAR = lambda it="string": {"type": ["array", "null"], "items": ({"type": it} if isinstance(it, str) else it)}
    ID = {"type": "string", "pattern": "^ds:[a-z0-9-]+:\\S+$"}
    OP = {"type": "object", "properties": {"op": {"type": "string"}, "authorization": {"type": "string"},
                                           "consent_required": {"type": "boolean"}}, "additionalProperties": True}
    cap = {"type": "object", "title": "platform.capability", "x-kind": "entity",
           "required": ["schema", "id", "title", "type", "publisher"],
           "properties": {"id": ID, "schema": {"const": "platform.capability"}, "tenant": NS(),
               "title": S(), "summary": NS(), "icon": NS(), "publisher": S(),
               "type": {"type": "string", "enum": ["information", "service", "disclosure", "dataset", "consultation",
                        "grievance", "subscription", "map", "directory",
                        "component", "solution", "model", "runtime", "agent"]},
               "jurisdiction": NS(), "audience": NAR("string"), "languages": NAR("string"),
               "effective_from": NS(), "effective_until": NS(),
               "target_kind": {"type": ["string", "null"],
                               "enum": ["app", "schema", "service", "publication", "case", "layer", "component", None]},
               "target_ref": NS(), "channels": NAR("string"), "operations": NAR(OP), "topics": NAR("string"),
               "schema_org_type": NS(), "source": NS(), "approval": NS(),
               "provenance_contract": NB(), "prov": NO()},
           "additionalProperties": False,
           "description": "A public capability — the discovery INDEX of the Agency Public Interface (ADR-0111). "
                          "References an existing record (app/schema/service/publication/case/layer/component); never "
                          "a copy. The unit a web composer AND an AI agent discover."}
    st = login("airawat")
    print("evolve platform.capability@0.2.0:",
          post(NET + "/v1/schemas", {"schema_id": "platform.capability", "version": "0.2.0",
                                     "definition": cap, "sensitivity": "public"}, st))


def cap(tenant, slug, title, type_, summary, icon, target_kind=None, target_ref=None, jurisdiction=None):
    o = {"schema": "platform.capability", "id": "ds:%s:platform/capability/%s" % (tenant, slug), "tenant": tenant,
         "title": title, "type": type_, "summary": summary, "icon": icon, "publisher": "ds:%s:steward" % tenant,
         "languages": ["en"], "channels": ["web", "machine"],
         "operations": [{"op": "discover", "authorization": "none", "consent_required": False}]}
    if target_kind:
        o["target_kind"] = target_kind
    if target_ref:
        o["target_ref"] = target_ref
    if jurisdiction:
        o["jurisdiction"] = jurisdiction
    return o


def prof(tenant, **kw):
    o = {"schema": "platform.agency_profile", "id": "ds:%s:platform/agency_profile/%s" % (tenant, tenant),
         "tenant": tenant, "languages": ["en"]}
    o.update(kw)
    return o


# ---- 2) per-tenant presence (real content) ----
PLAN = {
    "airawat": {
        "profile": prof("airawat", display_name="Airawat Network", legal_name="Airawat Network — Standards Body",
                        archetype="network", agency_type="Standards body", jurisdiction="India", emblem="🕸️",
                        primary_color="#111820", accent_color="#5a7fa0",
                        mandate="The governed catalog, participant directory and open standards for India's cities.",
                        about="Airawat is the network standards body: it holds the certified component catalog, the "
                              "schema and interface registries, and the signed participant directory. It governs; it "
                              "does not operate software or hold tenant data."),
        "caps": [
            cap("airawat", "marketplace", "Component Marketplace", "directory",
                "Certified components — apps, engines, drivers and AI agents — published by participants.", "🧩",
                "app", "components"),
            cap("airawat", "directory", "Participant Directory", "directory",
                "Every government, developer, operator and NGO on the network, with signed identity.", "🏛️"),
            cap("airawat", "schemas", "Schema Catalog", "directory",
                "The governed data-model catalog every participant conforms to.", "📐"),
            cap("airawat", "interfaces", "Interfaces & Verbs", "directory",
                "The governed interaction contracts (verbs) that components implement.", "🔌"),
        ]},
    "airawatdeveloper": {
        "profile": prof("airawatdeveloper", display_name="Airawat Developer",
                        legal_name="Airawat Reference Developer", archetype="developer", agency_type="Developer",
                        emblem="🛠️", primary_color="#1b1f3b", accent_color="#5b8def",
                        mandate="Reference components for the Airawat Network — apps, engines, drivers and AI agents.",
                        about="The reference developer: it authors and publishes the open components that other "
                              "participants install — the platform's own worked examples."),
        "caps": [
            cap("airawatdeveloper", "site-renderer", "Site Renderer", "component",
                "The governed public-site presentation adapter (ADR-0111).", "🖼️", "component",
                "ds:airawatdeveloper:engine/site-renderer"),
            cap("airawatdeveloper", "converse-engine", "Converse Engine", "component",
                "Grounded conversational answers over a tenant's governed data.", "💬", "component",
                "ds:airawatdeveloper:engine/converse-engine"),
            cap("airawatdeveloper", "telegram-concierge", "Telegram Concierge", "component",
                "A confined channel adapter — serve citizens over Telegram, token held in Vault.", "📨", "component",
                "ds:airawatdeveloper:adapter/telegram-concierge"),
            cap("airawatdeveloper", "air-quality-expert", "Air Quality Expert", "agent",
                "An AI expert for PM2.5 nowcast / forecast questions, grounded in governed air data.", "🌫️",
                "component", "ds:airawatdeveloper:agent/air-quality-expert"),
            cap("airawatdeveloper", "flood-expert", "Flood Expert", "agent",
                "An AI expert for flood-risk questions over the governed hydrology layers.", "🌊", "component",
                "ds:airawatdeveloper:agent/flood-expert"),
            cap("airawatdeveloper", "visualize", "Visualize (City Scan)", "component",
                "The map-first city explorer app — buildings, population, hydrology, air quality.", "🗺️", "component",
                "ds:airawatdeveloper:app/city-scan"),
        ]},
    "kmc": {
        "profile": prof("kmc", display_name="Kanpur Municipal Corporation", legal_name="Kanpur Municipal Corporation",
                        archetype="government", agency_type="Municipal corporation",
                        jurisdiction="Kanpur, Uttar Pradesh", emblem="🏛️",
                        primary_color="#0b3d6b", accent_color="#b4121b",
                        mandate="Municipal services, civic data and urban governance for the city of Kanpur.",
                        about="Kanpur Municipal Corporation's public portal — open civic data and city services, every "
                              "figure carrying its source."),
        "caps": [
            cap("kmc", "city-scan", "City Scan", "map",
                "Explore Kanpur — buildings, population, hydrology and air quality on one map.", "🗺️", "app",
                "city-scan", "Kanpur, Uttar Pradesh"),
            cap("kmc", "open-data", "Open City Data", "dataset",
                "Governed civic datasets for Kanpur, with provenance.", "📊", "app", "data", "Kanpur, Uttar Pradesh"),
        ]},
    "nashik": {
        "profile": prof("nashik", display_name="Nashik Municipal Corporation",
                        legal_name="Nashik Municipal Corporation", archetype="government",
                        agency_type="Municipal corporation", jurisdiction="Nashik, Maharashtra", emblem="🛕",
                        primary_color="#0b3d6b", accent_color="#c2410c",
                        mandate="Citizen services, civic data and the Kumbh-ready city baseline for Nashik.",
                        about="Nashik Municipal Corporation's public portal — apply for city services online and "
                              "explore the city's open civic baseline."),
        "caps": [
            cap("nashik", "city-scan", "City Scan", "map",
                "Explore Nashik — the Kumbh-ready city baseline: buildings, population, terrain and hydrology.", "🗺️",
                "app", "city-scan", "Nashik, Maharashtra"),
        ]},
    "new-delhi": {
        "profile": prof("new-delhi", display_name="New Delhi", legal_name="New Delhi Municipal Council",
                        archetype="government", agency_type="Municipal corporation", jurisdiction="New Delhi",
                        emblem="🏛️", primary_color="#0b3d6b", accent_color="#b4121b",
                        mandate="Public services and civic data for New Delhi.",
                        about="New Delhi's public portal — apply for municipal services online."),
        "caps": [
            cap("new-delhi", "citizen-services", "Citizen Services Portal", "service",
                "Apply for municipal services online and track their status.", "📄", "app", "jan-seva", "New Delhi"),
        ]},
}


def main():
    evolve_capability_schema()
    for tenant, spec in PLAN.items():
        try:
            tok = login(tenant)
        except Exception as e:
            print("  %-16s LOGIN FAILED: %s" % (tenant, str(e)[:80])); continue
        pr = put(tenant, spec["profile"], tok)
        print("  %-16s profile: %s" % (tenant, pr[0]))
        for c in spec["caps"]:
            r = put(tenant, c, tok)
            print("      cap %-22s %s" % (c["id"].split("/")[-1], r[0] if r[0] == "ok" else r))


if __name__ == "__main__":
    main()
