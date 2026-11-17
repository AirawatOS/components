#!/usr/bin/env python3
"""Display cards (viz.layer, render=card) for the service-workflow records an officer opens in Inbox, so they read
as cards instead of raw JSON. The kernel scopes viz.layer reads to the tenant, so each department gets its own copy
(ds:<tenant>:viz/layer/card-…), written by the department's fulfilment principal. Runs in the staging app-host
container (passwords from the server-side secrets file; never printed)."""
import json, urllib.request, urllib.error
IDN, K = "http://identity:8093", "http://kernel:8080"
F = lambda path, label: {"path": path, "label": label}
LAYERS = [
 {"layer_id": "card-platform-task", "source_schema": "platform.task", "label": "Task", "title_field": "title",
  "subtitle_field": "workflow_step",
  "fields": [F("workflow_step", "Step"), F("status", "Status"), F("assignee", "With"), F("created_at", "Received"),
             F("due_at", "Due"), F("case", "Case")]},
 {"layer_id": "card-platform-case", "source_schema": "platform.case", "label": "Case", "title_field": "title",
  "subtitle_field": "service",
  "fields": [F("service", "Service"), F("status", "Status"), F("current_step", "Current step"), F("opened_at", "Opened"),
             F("opened_by", "Applicant"), F("closed_at", "Closed")]},
 {"layer_id": "card-civic-submission", "source_schema": "civic.submission", "label": "Application", "title_field": "service",
  "fields": [F("service", "Service"), F("submitted_at", "Submitted"), F("holder", "Applicant")]},
]
def post(u, b, t=None):
    h = {"Content-Type": "application/json"}
    if t: h["Authorization"] = "Bearer " + t
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(u, data=json.dumps(b).encode(), headers=h, method="POST"), timeout=20).read()), None
    except urllib.error.HTTPError as e: return None, "%s %s" % (e.code, e.read().decode()[:200])
DEPTS = ["state-udd", "dlb", "housing-board", "sidc", "city-da", "uit", "city-mc"]
fp = json.load(open("/data/app-bundles/.secrets/fulfil-principals.json"))
for t in DEPTS:
  tok = post(IDN + "/login", {"username": fp[t]["username"], "tenant": t, "password": fp[t]["password"]})[0]["access_token"]
  bad = 0
  for i, L in enumerate(LAYERS):
    o = {"schema": "viz.layer", "id": "ds:%s:viz/layer/%s" % (t, L["layer_id"]), "tenant": t, "render": "card", "enabled": True, "order": i,
         "category": "other", "group": "Workflow", "value_field": "status", "color_scale": "blues",
         "solution": "urban-services", **L}
    r, e = post(K + "/v1/registry/objects", {"id": o["id"], "object": o}, tok)
    if not r: bad += 1; print("  ", o["id"], "FAILED", e)
  print("%-14s %d card layers%s" % (t, len(LAYERS) - bad, "" if not bad else " (%d failed)" % bad))
