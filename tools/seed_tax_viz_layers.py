#!/usr/bin/env python3
"""Author the Property-Tax-Gap viz.layer descriptors (shared catalogue, ds:airawat) so City Scan lists them for
any tenant carrying the data. Modeled on the live air-* layer shape. Run inside airawat-app-host. Env: IDN KERNEL."""
import os, json, urllib.request, urllib.error
IDN=os.environ.get("IDN","http://airawat-identity:8093"); KERNEL=os.environ.get("KERNEL","http://kernel:8080")
def login(r,u):
    q=urllib.request.Request(f"{IDN}/realms/{r}/login",data=json.dumps({"username":u}).encode(),headers={"Content-Type":"application/json"},method="POST")
    return json.loads(urllib.request.urlopen(q,timeout=15).read())["access_token"]
tok=login("airawat","steward")
def put(oid,obj):
    q=urllib.request.Request(KERNEL+"/v1/registry/objects",data=json.dumps({"id":oid,"object":obj}).encode(),
        headers={"Content-Type":"application/json","Authorization":"Bearer "+tok},method="POST")
    try:
        urllib.request.urlopen(q,timeout=30).read(); return "OK"
    except urllib.error.HTTPError as e: return f"{e.code} {e.read().decode()[:200]}"

LAYERS=[
 {"id":"ds:airawat:viz/layer/tax-unregistered","layer_id":"tax-unregistered","schema":"viz.layer",
  "label":"Unregistered properties (tax gap)","short_label":"Unregistered","group":"Property Tax","subgroup":"Enforcement",
  "order":30,"render":"map","category":"built","solution":"tax-gap-finder","color_scale":"reds","value_field":"unregistered_count",
  "source_schema":"cell.tax_gap","unit":"","enabled":True,"higher_is_worse":True,
  "description":"Surveyed built properties with NO matching record in the property-tax register — candidates for field verification.",
  "interpretation":"Each point is a property the GIS survey found but the tax register does not carry. Confidence reflects how many independent signals fired (unregistered footprint, utility with no tax, satellite new-build)."},
 {"id":"ds:airawat:viz/layer/tax-surveyed","layer_id":"tax-surveyed","schema":"viz.layer",
  "label":"Surveyed properties (cadastre)","short_label":"Surveyed","group":"Property Tax","subgroup":"Enforcement",
  "order":31,"render":"map","category":"built","solution":"tax-gap-finder","color_scale":"blues","value_field":"surveyed_count",
  "source_schema":"cell.tax_gap","unit":"","enabled":True,"higher_is_worse":False,
  "description":"All built properties from the GIS property survey — the cadastral base the tax gap is measured against.",
  "interpretation":"The full universe of surveyed built properties. Those NOT lit up as unregistered are the ones the engine matched to a tax-register record."},
]
for L in LAYERS:
    print(L["layer_id"], "->", put(L["id"], L))
