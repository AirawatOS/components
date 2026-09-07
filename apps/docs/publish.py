#!/usr/bin/env python3
"""Certify + publish the Docs app (ds:airawat:app/docs) to a network. Mirrors app_host _bridge_project_publish:
hash files -> /v1/certify -> /v1/store/publish (declares read/write scopes from meta). Publishes as the AIRAWAT
steward (the namespace of the component_id). Run where NET/IDN resolve (e.g. inside airawat-app-host).
Usage: NET=http://airawat-network:8090 IDN=http://identity:8093 APPDIR=/app/components/apps/docs python3 publish.py"""
import json, urllib.request, urllib.error, os, hashlib, sys

NET = os.environ.get("NET", "http://airawat-network:8090"); IDN = os.environ.get("IDN", "http://identity:8093")
APPDIR = os.environ.get("APPDIR", os.path.dirname(os.path.abspath(__file__)))

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

m = read_meta(APPDIR); cid = m["component_id"]
# certify/publish AS THE STEWARD OF THE COMPONENT'S NAMESPACE (ds:<ns>:…): airawatdeveloper→devrick, else airawat→steward
_ns = cid.split(":")[1] if cid.startswith("ds:") else "airawat"
TOKEN = login("airawatdeveloper", "devrick") if _ns == "airawatdeveloper" else login("airawat", "steward")
meta_yaml = open(os.path.join(APPDIR, "meta.yaml")).read(); files = files_of(APPDIR)
cert, err = _post(f"{NET}/v1/certify", {"subject_id": cid, "subject_kind": "component",
                  "meta_yaml": meta_yaml, "files": files}, TOKEN)
if err: sys.exit(f"✗ certify {cid}: {err}")
conf = cert.get("certificate", {}).get("conformance", {})
writes = m.get("writes", []); reads = m.get("required_schemas", [])
writes = writes if isinstance(writes, list) else [writes]
reads = reads if isinstance(reads, list) else [reads]
scopes = [{"verb": "write", "scope": s} for s in writes] + [{"verb": "read", "scope": s} for s in reads]
pub, perr = _post(f"{NET}/v1/store/publish", {"component_id": cid, "kind": m["kind"],
                  "version": m.get("version", "0.1.0"), "provider": m.get("provider", ""), "scopes": scopes}, TOKEN)
# Re-certifying the SAME version to refresh the admission digest (bundle content changed, version unchanged) is
# fine — the store record already exists, so an "already published" is expected and not fatal; certify above
# already updated the evidence digest the app-host verifies against.
if perr and not any(w in perr.lower() for w in ("already", "exists", "409")):
    sys.exit(f"✗ publish {cid}: {perr}")
print(f"✓ {cid} certified(verified={conf.get('verified')}) {'+ published' if not perr else '(digest refreshed; store record kept)'}  scopes={[s['scope'] for s in scopes]}")
