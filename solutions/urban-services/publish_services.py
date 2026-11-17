#!/usr/bin/env python3
"""Publish urban service offerings (services/*.json) to the network Service Directory, each as its own
department. Runs INSIDE the staging app-host container: signs in as the department's fulfilment principal
(<dept>-service, password from the server-side secrets file — never printed). Usage:
  docker exec -i airawat-app-host python3 /tmp/publish_services.py < offerings.jsonl"""
import json, sys, urllib.error, urllib.request

NET, IDN = "http://network:8090", "http://identity:8093"
SECRETS = "/data/app-bundles/.secrets/fulfil-principals.json"


def post(u, b, t=None):
    h = {"Content-Type": "application/json"}
    if t: h["Authorization"] = "Bearer " + t
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(u, data=json.dumps(b).encode(), headers=h, method="POST"), timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, "%s %s" % (e.code, e.read().decode()[:300])


fp = json.load(open(SECRETS))
for line in sys.stdin.read().splitlines():
    if not line.strip(): continue
    off = json.loads(line); t = off["tenant"]
    if t not in fp:
        print("%-24s SKIP: no fulfilment principal for %s" % (off["service_id"], t)); continue
    r, e = post(IDN + "/login", {"username": fp[t]["username"], "tenant": t, "password": fp[t]["password"]})
    tok = (r or {}).get("access_token")
    if not tok:
        print("%-24s login FAILED %s" % (off["service_id"], (e or "")[:3])); continue
    r, e = post(NET + "/v1/service-offerings", off, tok)
    print("%-24s %s" % (off["service_id"], ("published " + r["published"]) if r else "FAILED " + e))
