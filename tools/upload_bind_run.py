#!/usr/bin/env python3
"""Land a data file for a driver + fire its run (run inside airawat-app-host). Mirrors the /bridge/asset flow:
upload FILEPATH → filestore → write platform.component_asset(asset_name=<config env key>) for TENANT → POST
scheduler /run {component, tenant}. Env: IDN NET KERNEL FILESTORE SCHED TENANT COMPONENT ASSET_NAME FILEPATH."""
import os, io, json, time, urllib.request, urllib.error

IDN = os.environ["IDN"]; KERNEL = os.environ.get("KERNEL", "http://kernel:8080")
FILESTORE = os.environ.get("FILESTORE", "http://filestore:8095"); SCHED = os.environ.get("SCHED", "http://airawat-scheduler:8103")
TENANT = os.environ["TENANT"]; COMPONENT = os.environ["COMPONENT"]; ASSET = os.environ["ASSET_NAME"]; FP = os.environ["FILEPATH"]

def login(realm, user):
    r = urllib.request.Request(f"{IDN}/realms/{realm}/login", data=json.dumps({"username": user}).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=15).read())["access_token"]

def post(url, body, tok, ctype="application/json"):
    data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
    h = {"Content-Type": ctype}
    if tok: h["Authorization"] = "Bearer " + tok
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method="POST"), timeout=120).read()

tok = login(TENANT, TENANT + "-admin")   # writing TENANT's component_asset needs a TENANT admin (cross-tenant guard)
blob = open(FP, "rb").read()
fid = json.loads(post(FILESTORE + "/files", blob, None, "application/octet-stream"))["id"]
print(f"  filestore {fid} ({len(blob)} bytes)")
slug = "".join(c if c.isalnum() else "-" for c in COMPONENT.split(":")[-1]).strip("-")
oid = f"ds:{TENANT}:platform/component_asset/{slug}--{ASSET}"
rec = {"schema": "platform.component_asset", "id": oid, "tenant": TENANT, "component": COMPONENT,
       "asset_name": ASSET, "populates": ASSET,   # scheduler injects env[populates]=file_id at run time
       "file_id": fid, "filename": os.path.basename(FP),
       "size_bytes": len(blob), "content_type": "text/csv", "uploaded_by": "ptgap-s1b",
       "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
try:
    post(KERNEL + "/v1/registry/objects", {"id": oid, "object": rec}, tok); print("  component_asset:", oid, "asset_name=" + ASSET)
except urllib.error.HTTPError as e:
    print("  component_asset ERR", e.code, e.read()[:200]); raise SystemExit(1)
try:
    r = post(SCHED + "/run", {"component": COMPONENT, "tenant": TENANT, "reason": "ptgap-s1b-test"}, tok)
    print("  scheduler /run ->", r[:200])
except urllib.error.HTTPError as e:
    print("  scheduler /run ERR", e.code, e.read()[:200])
