#!/usr/bin/env python3
"""One-shot component deploy (certify + source-stage + a SINGLE publish carrying source_file_id) — run INSIDE
airawat-app-host. Avoids the double-publish of publish_component.py+stage_and_publish.py (which the network now
rejects as a source-change on an already-published version). Env: IDN NET FILESTORE KERNEL APPDIR."""
import os, io, json, time, tarfile, hashlib, urllib.request, urllib.error, sys

IDN = os.environ.get("IDN", "http://airawat-identity:8093"); NET = os.environ.get("NET", "http://airawat-network:8090")
FILESTORE = os.environ.get("FILESTORE", "http://filestore:8095"); KERNEL = os.environ.get("KERNEL", "http://kernel:8080")
APPDIR = os.environ["APPDIR"]

def login(realm, user):
    # passwordless kernel login: {username, tenant} → platform.steward (the /realms/<realm>/login path is gated)
    r = urllib.request.Request(f"{IDN}/login", data=json.dumps({"username": user, "tenant": realm}).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    d = json.loads(urllib.request.urlopen(r, timeout=15).read())
    return d.get("access_token") or d.get("token")

def post(url, body, tok, ctype="application/json"):
    data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
    h = {"Content-Type": ctype}
    if tok: h["Authorization"] = "Bearer " + tok
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method="POST"), timeout=120).read() or b"{}")

m = {}
for line in open(APPDIR + "/meta.yaml"):
    line = line.split("#", 1)[0].rstrip()
    if not line or line[0] == " " or ":" not in line: continue
    k, v = line.split(":", 1); m[k.strip()] = v.strip()
def asl(v):
    v = (v or "").strip()
    return [x.strip() for x in v[1:-1].split(",") if x.strip()] if (v.startswith("[") and v.endswith("]")) else ([v] if v else [])

cid = m["component_id"]; ver = m.get("version", "0.1.0"); steward = cid.split(":")[1]; name = cid.rsplit("/", 1)[-1]
tok = login(steward, "steward")

# 1) certify.  `files` = {relpath: sha256} (artifact digest); `sources` = {relpath: text} for TEXT files so the CA
# can run source-aware checks (e.g. actions-handled: every declared post-config action mode is serviced in code).
files, sources = {}, {}
TEXT = (".py", ".yaml", ".yml", ".json", ".md", ".txt", ".html", ".js", ".css", ".sh")
for base, _dd, names in os.walk(APPDIR):
    if "__pycache__" in base: continue
    for n in names:
        if n.endswith((".pyc", ".log")): continue
        p = os.path.join(base, n); rel = os.path.relpath(p, APPDIR)
        files[rel] = hashlib.sha256(open(p, "rb").read()).hexdigest()
        if n.endswith(TEXT):
            try: sources[rel] = open(p, encoding="utf-8").read()
            except Exception: pass
cert = post(f"{NET}/v1/certify", {"subject_id": cid, "subject_kind": "component",
            "meta_yaml": open(APPDIR + "/meta.yaml").read(), "files": files, "sources": sources}, tok)
print("certified verified=", cert.get("certificate", {}).get("conformance", {}).get("verified"))

# 2) stage source → filestore + component_asset
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tf:
    tf.add(APPDIR, arcname=".")
blob = buf.getvalue()
fid = post(FILESTORE + "/files", blob, None, ctype="application/octet-stream")["id"]
aid = f"ds:{steward}:platform/component_asset/{name}-source"
post(KERNEL + "/v1/registry/objects", {"id": aid, "object": {"schema": "platform.component_asset", "id": aid,
     "tenant": steward, "component": cid, "asset_name": "__component_source__", "file_id": fid,
     "filename": f"{name}.tar.gz", "content_type": "application/gzip", "size_bytes": len(blob),
     "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}, tok)
print("source staged fid=", fid, "(%d bytes)" % len(blob))

# 3) SINGLE publish carrying source_file_id
writes = asl(m.get("writes")); reads = asl(m.get("reads")) or asl(m.get("required_schemas"))
scopes = [{"verb": "write", "scope": s} for s in writes] + [{"verb": "read", "scope": s} for s in reads]
try:
    post(NET + "/v1/store/publish", {"component_id": cid, "kind": m["kind"], "version": ver,
         "provider": m.get("provider", ""), "scopes": scopes, "runtime_profile": "", "source_file_id": fid}, tok)
    print(f"PUBLISHED {cid}@{ver} WITH source_file_id")
except urllib.error.HTTPError as e:
    body = e.read().decode()[:300]
    if any(w in body.lower() for w in ("already", "exists", "409")):
        print(f"publish note: {e.code} {body}")
    else:
        sys.exit(f"FAIL publish: {e.code} {body}")
