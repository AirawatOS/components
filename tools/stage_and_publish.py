#!/usr/bin/env python3
"""Self-contained source staging + publish, run INSIDE airawat-app-host (avoids hostcore cfg's localhost:8080
which is unreachable from a bare exec). Mirrors _store_bundle: tar.gz(arcname='.') -> filestore /files -> write
platform.component_asset(__component_source__) via the kernel -> store/publish WITH source_file_id.
Env: IDN NET FILESTORE KERNEL APPDIR."""
import os, io, json, time, tarfile, urllib.request, urllib.error

IDN = os.environ["IDN"]; NET = os.environ["NET"]
FILESTORE = os.environ.get("FILESTORE", "http://filestore:8095")
KERNEL = os.environ.get("KERNEL", "http://kernel:8080")
APPDIR = os.environ["APPDIR"]

def login(realm, user):
    r = urllib.request.Request(f"{IDN}/realms/{realm}/login", data=json.dumps({"username": user}).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=15).read())["access_token"]

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

cid = m["component_id"]; ver = m.get("version", "0.1.0")
steward = cid.split(":")[1]; name = cid.rsplit("/", 1)[-1]
tok = login("airawatdeveloper", "devrick")

buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tf:
    tf.add(APPDIR, arcname=".")
blob = buf.getvalue()
fid = post(FILESTORE + "/files", blob, None, ctype="application/octet-stream")["id"]
print("filestore fid =", fid, "(%d bytes)" % len(blob))

aid = f"ds:{steward}:platform/component_asset/{name}-source"
rec = {"schema": "platform.component_asset", "id": aid, "tenant": steward, "component": cid,
       "asset_name": "__component_source__", "file_id": fid, "filename": f"{name}.tar.gz",
       "content_type": "application/gzip", "size_bytes": len(blob),
       "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
try:
    post(KERNEL + "/v1/registry/objects", {"id": aid, "object": rec}, tok); print("component_asset written:", aid)
except urllib.error.HTTPError as e:
    print("component_asset ERR:", e.code, e.read()[:200])

writes = asl(m.get("writes")); reads = asl(m.get("reads")) or asl(m.get("required_schemas"))
scopes = [{"verb": "write", "scope": s} for s in writes] + [{"verb": "read", "scope": s} for s in reads]
body = {"component_id": cid, "kind": m["kind"], "version": ver, "provider": m.get("provider", ""),
        "scopes": scopes, "runtime_profile": "", "source_file_id": fid}
try:
    post(NET + "/v1/store/publish", body, tok); print("published WITH source_file_id:", fid)
except urllib.error.HTTPError as e:
    print("publish ERR:", e.code, e.read()[:200])
