"""Fan out the Compute plane (ADR-0091 #267): seed a governed RECIPE (platform.verb_alias) for each pure-compute
handler + deprecate its now-superseded domain interface → compute.run. Non-breaking (bespoke verbs still answer;
recipes are additive; deprecation is an annotation). Idempotent. Runs inside app-host."""
import json, urllib.request as u, urllib.error
IDN = "http://identity:8093"; NET = "http://network:8090"; KRN = "http://kernel:8080"

def post(url, b, h=None):
    r = u.Request(url, data=json.dumps(b).encode(), headers={"Content-Type": "application/json", **(h or {})}, method="POST")
    try: return json.loads(u.urlopen(r, timeout=25).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code}:{e.read().decode()[:120]}"

# (recipe id, compute handler verb, domain interface to deprecate)
RECIPES = [
    ("welfare.gap-scan",          "welfare-gap-scan",          "welfare.gap_scan"),
    ("welfare.prioritise",        "welfare-prioritise",        "welfare.prioritise"),
    ("welfare.prioritise-cells",  "welfare-prioritise-cells",  "welfare.prioritise_cells"),
    ("welfare.deprivation-scan",  "welfare-deprivation-scan",  "welfare.deprivation_scan"),
    ("tax.gap-scan",              "gap-scan",                  "tax.gap_scan"),
    ("tax.match-preview",         "match-preview",             "tax.match_preview"),
    ("project.rollup",            "project-rollup",            "project.rollup"),
    ("project.review",            "project-review",            "project.review"),
    ("project.evaluate",          "project-evaluate",          "project.evaluate"),
]

stok = (post(f"{IDN}/login", {"username": "steward", "tenant": "airawat"})[0] or {}).get("access_token")
H = {"Authorization": "Bearer " + stok}
for rid, handler, iface in RECIPES:
    rec = {"schema": "platform.verb_alias", "id": f"ds:airawat:platform/verb_alias/{rid}",
           "tenant": "airawat", "alias": rid, "enabled": True, "target_verb": handler}
    r1, e1 = post(f"{KRN}/v1/registry/objects", {"id": rec["id"], "object": rec}, H)
    r2, e2 = post(f"{NET}/v1/interfaces/deprecate",
                  {"interface_id": iface, "version": "0.1.0", "superseded_by": "compute.run@0.1.0",
                   "reason": "ADR-0091 Compute plane: served by generic run(recipe)"}, H)
    print(f"  {rid:26s} recipe={'ok' if r1 else e1:4s}  deprecate {iface:26s}={'ok' if r2 else e2}")

d = json.loads(u.urlopen(NET + "/v1/interfaces?status=deprecated", timeout=10).read())
tot = json.loads(u.urlopen(NET + "/v1/interfaces", timeout=10).read())
print(f"\ndeprecated: {len(d['interfaces'])} / {len(tot['interfaces'])} total  |  active: {sum(1 for i in tot['interfaces'] if i.get('status','active')=='active')}")
