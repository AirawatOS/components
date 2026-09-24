#!/usr/bin/env python3
"""List studio.project records (airawatdeveloper) and add MEMBER to the one whose title matches MATCH (substring,
case-insensitive). Run inside airawat-app-host. Env: IDN KERNEL MATCH MEMBER."""
import os, json, urllib.request, urllib.error

IDN = os.environ.get("IDN", "http://airawat-identity:8093"); KERNEL = os.environ.get("KERNEL", "http://kernel:8080")
MATCH = os.environ.get("MATCH", "property tax"); MEMBER = os.environ.get("MEMBER", "")

def login(realm, user):
    r = urllib.request.Request(f"{IDN}/realms/{realm}/login", data=json.dumps({"username": user}).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=15).read())["access_token"]

def post(url, body, tok):
    h = {"Content-Type": "application/json"}
    if tok: h["Authorization"] = "Bearer " + tok
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(),
                          headers=h, method="POST"), timeout=60).read() or b"{}"), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code} {e.read().decode()[:200]}"

tok = login("airawatdeveloper", "devrick")
r, e = post(KERNEL + "/v1/registry/resolve", {"filter": {"schema": "studio.project"}}, tok)
rows = (r or {}).get("results") or (r or {}).get("objects") or (r if isinstance(r, list) else [])
projs = [(x.get("object") or x) for x in rows]
projs = [p for p in projs if p.get("schema") == "studio.project"]
print(f"studio.project count: {len(projs)}")
for p in projs:
    print("  -", p.get("id"), "|", repr(p.get("title")), "| members:", len(p.get("members") or []))
target = next((p for p in projs if MATCH.lower() in (p.get("title") or "").lower()), None)
if not target:
    print(f"\nNO project title contains {MATCH!r}"); raise SystemExit(0)
def ref_of(m):
    return (m.get("ref") or m.get("component_id")) if isinstance(m, dict) else m
mem = target.get("members") or []
# drop any prior malformed entry for this member (wrong key), then append the correct `ref`-shaped record
mem = [m for m in mem if ref_of(m) != MEMBER]
print(f"\nTARGET {target['id']} title={target.get('title')!r}\n  members: {[ref_of(m) for m in mem]}")
if MEMBER:
    mem.append({"ref": MEMBER, "kind": "engine", "layer": "domain",
                "title": "Tax Gap Engine", "origin": "existing", "status": "published"})
    target["members"] = mem
    w, we = post(KERNEL + "/v1/registry/objects", {"id": target["id"], "object": target}, tok)
    print("  ADD ERR" if we else f"  ADDED {MEMBER}", we or "-> members now: " + str([ref_of(m) for m in mem]))
