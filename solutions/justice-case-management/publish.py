#!/usr/bin/env python3
"""Certify + publish the Court Console app and the Court Case Management solution to a network.
Mirrors app_host _bridge_project_publish: hash files -> /v1/certify (verified conformance) -> /v1/store/publish
(kind=solution runs the ADR-0075 self-contained-closure gate). Publishes as devrick (steward of airawatdeveloper).
Usage: NET=... IDN=... python3 publish.py
"""
import json, urllib.request, urllib.error, os, hashlib, sys

NET = os.environ.get("NET", "http://localhost:8090"); IDN = os.environ.get("IDN", "http://localhost:8093")
COMP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo/components

def _post(url, body, token):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token}, method="POST")
    try: return json.loads(urllib.request.urlopen(req, timeout=60).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:400]}"

def login(realm, user):
    req = urllib.request.Request(f"{IDN}/realms/{realm}/login", data=json.dumps({"username": user}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=15).read())["access_token"]

# minimal meta parse (no yaml dep)
def read_meta(d):
    m = {}
    for line in open(os.path.join(d, "meta.yaml")):
        line = line.split("#", 1)[0].rstrip()
        if not line or line[0] == " " or ":" not in line: continue
        k, v = line.split(":", 1); k = k.strip(); v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            v = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        m[k] = v
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

DTOKEN = login("airawatdeveloper", "devrick")

def publish(d):
    m = read_meta(d); cid = m["component_id"]; kind = m["kind"]
    meta_yaml = open(os.path.join(d, "meta.yaml")).read(); files = files_of(d)
    cert, err = _post(f"{NET}/v1/certify", {"subject_id": cid, "subject_kind": "component",
                      "meta_yaml": meta_yaml, "files": files}, DTOKEN)
    if err: print(f"  ✗ certify {cid}: {err}"); return False
    conf = cert.get("certificate", {}).get("conformance", {})
    scopes = None
    if kind == "app":
        writes = m.get("writes", []); reads = m.get("required_schemas", [])
        writes = writes if isinstance(writes, list) else [writes]
        reads = reads if isinstance(reads, list) else [reads]
        scopes = [{"verb": "write", "scope": s} for s in writes] + [{"verb": "read", "scope": s} for s in reads]
    members = m.get("members")
    if isinstance(members, str): members = [members]
    pub, perr = _post(f"{NET}/v1/store/publish", {"component_id": cid, "kind": kind,
                      "version": m.get("version", "0.1.0"), "provider": m.get("provider", ""),
                      "scopes": scopes, "members": members}, DTOKEN)
    if perr: print(f"  ✗ publish {cid}: {perr}"); return False
    print(f"  ✓ {cid}  certified(verified={conf.get('verified')}) + published")
    return True

print("== publish court-console app ==")
ok1 = publish(os.path.join(COMP, "apps", "court-console"))
print("== publish justice-case-management solution (runs closure gate) ==")
ok2 = publish(os.path.join(COMP, "solutions", "justice-case-management"))
sys.exit(0 if (ok1 and ok2) else 1)
