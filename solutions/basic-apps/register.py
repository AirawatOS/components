#!/usr/bin/env python3
"""Certify + publish + register the Basic Apps solution (ds:airawat:solution/basic-apps) on a network.
Mirrors the compose path: /v1/certify -> /v1/store/publish (kind=solution) -> /v1/solutions (sign the member
graph so it lists in the Marketplace "Solutions" tab and can fan-out install). Published AS the AIRAWAT steward.
Run where NET/IDN resolve (e.g. inside airawat-app-host):
    NET=http://airawat-network:8090 IDN=http://identity:8093 SOLDIR=/app/components/solutions/basic-apps python3 register.py
"""
import json, urllib.request, urllib.error, os, hashlib, sys

NET = os.environ.get("NET", "http://airawat-network:8090"); IDN = os.environ.get("IDN", "http://identity:8093")
SOLDIR = os.environ.get("SOLDIR", os.path.dirname(os.path.abspath(__file__)))

def _post(url, body, token):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token}, method="POST")
    try: return json.loads(urllib.request.urlopen(req, timeout=60).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:400]}"

def login(realm, user):
    req = urllib.request.Request(f"{IDN}/realms/{realm}/login", data=json.dumps({"username": user}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=15).read())["access_token"]

def read_meta(d):
    m, members, in_members = {}, [], False
    for line in open(os.path.join(d, "meta.yaml")):
        raw = line.rstrip("\n")
        s = raw.split("#", 1)[0].rstrip()
        st = s.strip()
        if st.startswith("- "):                       # list item (members:)
            if in_members: members.append(st[2:].strip())
            continue
        if not s or ":" not in s:
            in_members = False if (raw and not raw[0].isspace()) else in_members
            continue
        if not raw[0].isspace():                       # top-level key
            k, v = s.split(":", 1); k = k.strip(); v = v.strip()
            in_members = (k == "members")
            if not in_members: m[k] = v
    m["members"] = members
    return m

def files_of(d):
    files = {}
    for base, _dd, names in os.walk(d):
        if "__pycache__" in base: continue
        for n in names:
            if n.endswith((".pyc", ".log")): continue
            p = os.path.join(base, n)
            files[os.path.relpath(p, d)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return files

m = read_meta(SOLDIR); cid = m["component_id"]; ver = m.get("version", "0.1.0")
_ns = cid.split(":")[1] if cid.startswith("ds:") else "airawat"
TOKEN = login("airawatdeveloper", "devrick") if _ns == "airawatdeveloper" else login("airawat", "steward")
meta_yaml = open(os.path.join(SOLDIR, "meta.yaml")).read(); files = files_of(SOLDIR)

# certify + store-publish are BEST-EFFORT: the network authority (airawat) may not certify software/solutions
# through the store (ADR-0094) — a platform solution is delivered by its baked/overlay member apps, so a 403 here
# is expected and non-fatal. The member-graph registration below is what makes it a real, resolvable solution.
cert, err = _post(f"{NET}/v1/certify", {"subject_id": cid, "subject_kind": "component",
                  "meta_yaml": meta_yaml, "files": files}, TOKEN)
if err: print(f"  · certify skipped ({err[:80]})")
else:
    pub, perr = _post(f"{NET}/v1/store/publish", {"component_id": cid, "kind": "solution",
                      "version": ver, "provider": m.get("provider", ""), "scopes": []}, TOKEN)
    if perr and not any(w in perr.lower() for w in ("already", "exists", "409")):
        print(f"  · store-publish skipped ({perr[:80]})")

reg, rerr = _post(f"{NET}/v1/solutions", {"solution_id": cid, "title": m.get("display_name", cid),
                  "version": ver, "members": m["members"]}, TOKEN)
if rerr: sys.exit(f"✗ register {cid}: {rerr}")
print(f"✓ {cid}@{ver} registered  members={m['members']}")
