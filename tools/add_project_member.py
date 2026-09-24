#!/usr/bin/env python3
"""Add a component to a studio.project's members (server-side), run inside airawat-app-host.
Env: IDN NET KERNEL PROJECT_TITLE MEMBER."""
import os, json, urllib.request, urllib.error

IDN = os.environ["IDN"]; NET = os.environ["NET"]; KERNEL = os.environ.get("KERNEL", "http://kernel:8080")
TITLE = os.environ["PROJECT_TITLE"]; MEMBER = os.environ["MEMBER"]

def login(realm, user):
    r = urllib.request.Request(f"{IDN}/realms/{realm}/login", data=json.dumps({"username": user}).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=15).read())["access_token"]

def post(url, body, tok):
    h = {"Content-Type": "application/json"}
    if tok: h["Authorization"] = "Bearer " + tok
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST"), timeout=30).read() or b"{}")

tok = login("airawatdeveloper", "devrick")
rows = []
for base in (NET, KERNEL):
    for path in ("/v1/query", "/v1/registry/query"):
        try:
            r = post(base + path, {"scope": "studio.project", "limit": 500}, tok)
            rows = r.get("results") or r.get("objects") or r.get("rows") or []
            if rows:
                print("queried", base + path, "->", len(rows), "projects"); break
        except Exception as e:
            pass
    if rows: break
proj = None
for row in rows:
    o = row.get("object") or row
    if (o.get("title") or "").strip() == TITLE:
        proj = o; break
if not proj:
    print("PROJECT NOT FOUND via query; titles seen:", [ (row.get("object") or row).get("title") for row in rows ][:20]); raise SystemExit(1)
pid = proj["id"]; members = proj.get("members") or []
print("project", pid, "members before:", members)
if MEMBER not in members:
    members.append(MEMBER)
    proj["members"] = members
    post(KERNEL + "/v1/registry/objects", {"id": pid, "object": proj}, tok)
    print("ADDED", MEMBER, "-> members now:", members)
else:
    print("already a member")
