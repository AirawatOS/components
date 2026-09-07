#!/usr/bin/env python3
"""Register the Airawat Workspace (office productivity suite) as a Studio PROJECT under airawatdeveloper —
so the suite shows up in Solution Studio as a first-class, layered project (ADR-0037), like the Justice one.
Members = the 6 apps (interaction) + their governed schemas (domain / platform). Run where IDN/K resolve
(e.g. inside airawat-app-host)."""
import urllib.request, urllib.error, json, os, time
IDN = os.environ.get("IDN", "http://identity:8093"); K = os.environ.get("K", "http://airawat-kernel:8080")

def login(realm, u):
    r = urllib.request.Request(f"{IDN}/realms/{realm}/login", data=json.dumps({"username": u}).encode(),
                               headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=10).read())["access_token"]
D = login("airawatdeveloper", "devrick")

def app(ref, title, reads, writes):
    return {"ref": ref, "kind": "app", "layer": "interaction", "title": title, "origin": "new",
            "status": "published", "reads": reads, "writes": writes}
def sch(name, ver="0.1.0"):
    return {"ref": f"ds:airawat:schema/{name}@{ver}", "kind": "schema", "title": name,
            "origin": "new", "status": "published", "reads": [], "writes": []}

members = [
    app("ds:airawat:app/docs",   "Docs",   ["doc.document"], ["doc.document"]),
    app("ds:airawat:app/sheets", "Sheets", ["doc.sheet"],    ["doc.sheet"]),
    app("ds:airawat:app/slides", "Slides", ["doc.deck"],     ["doc.deck"]),
    app("ds:airawat:app/chat",   "Chat",   ["chat.channel", "chat.message"], ["chat.channel", "chat.message"]),
    app("ds:airawat:app/mail",   "Mail",   ["mail.message"], ["mail.message"]),
    app("ds:airawat:app/inbox",  "Tasks",  ["platform.inbox_item"], ["platform.inbox_item"]),
    sch("doc.document"), sch("doc.sheet"), sch("doc.deck"),
    sch("chat.channel"), sch("chat.message"), sch("mail.message"),
    sch("platform.inbox_item"),
]

pid = "ds:airawatdeveloper:studio/project/workspace-suite"
proj = {"schema": "studio.project", "id": pid, "tenant": "airawatdeveloper",
        "title": "Airawat Workspace (Office Suite)",
        "goal": "The base-OS productivity suite every tenant receives — Docs, Sheets, Slides, Chat, Mail (+ Tasks) "
                "— all AI-native over governed data. Personal apps are owner-scoped; Chat/Mail are membership-"
                "scoped; every app's AI routes through the shared /bridge/ai-assist (Kimi K3).",
        "owner": "user://airawatdeveloper/devrick", "status": "active",
        "brief": {"users": ["every signed-in tenant user"],
                  "entities": ["document", "spreadsheet", "presentation", "chat channel/message",
                               "mail message", "task/inbox item"],
                  "data_sources": ["human-entered content (user-initiated)"],
                  "decisions": [], "triggers": ["form submit", "message send", "AI assist"]},
        "members": members, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

r = urllib.request.Request(f"{K}/v1/registry/objects", data=json.dumps({"id": pid, "object": proj}).encode(),
                           method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer " + D})
try:
    out = json.loads(urllib.request.urlopen(r, timeout=15).read())
    print("stored:", out.get("stored"), "wrote_by:", out.get("wrote_by"), "| members:", len(members))
except urllib.error.HTTPError as e:
    print("ERR", e.code, e.read().decode()[:300])
