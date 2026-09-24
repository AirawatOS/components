#!/usr/bin/env python3
"""Install a component into TENANT (kernel /v1/components/install as tenant admin) + fire its run.
Run inside airawat-app-host. Env: IDN KERNEL SCHED TENANT COMPONENT."""
import os, json, urllib.request, urllib.error

IDN = os.environ["IDN"]; KERNEL = os.environ.get("KERNEL", "http://kernel:8080")
SCHED = os.environ.get("SCHED", "http://airawat-scheduler:8103"); TENANT = os.environ["TENANT"]; COMPONENT = os.environ["COMPONENT"]

def login(realm, user):
    r = urllib.request.Request(f"{IDN}/realms/{realm}/login", data=json.dumps({"username": user}).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=15).read())["access_token"]

def post(url, body, tok):
    h = {"Content-Type": "application/json"}
    if tok: h["Authorization"] = "Bearer " + tok
    return urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST"), timeout=120).read()

tok = login(TENANT, TENANT + "-admin")
try:
    r = post(KERNEL + "/v1/components/install", {"component_id": COMPONENT}, tok)
    print("  install ->", r[:200])
except urllib.error.HTTPError as e:
    print("  install ERR", e.code, e.read()[:300])
try:
    r = post(SCHED + "/run", {"component": COMPONENT, "tenant": TENANT, "reason": "ptgap-s1b-test"}, tok)
    import json as _j
    d = _j.loads(r); print("  run status=", d.get("status"), "exit=", d.get("exit")); print("  LOG:\n" + (d.get("log") or "")[:3000])
except urllib.error.HTTPError as e:
    print("  run ERR", e.code, e.read()[:300])
