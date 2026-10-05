"""Create the Studio project for the PED under janaagraha, pre-populated with the component members derived
from ds:janaagraha:service_spec/public-expenditure-dashboard. Drivers write the governed nouns, an engine
reconciles/flags, two apps consume. Written as the janaagraha writer; appears in janaagraha-dev's Studio."""
import json, os, urllib.request, urllib.error
IDN="http://identity:8093"; KERNEL="http://kernel:8080"; TENANT="janaagraha"
PID="ds:janaagraha:studio/project/public-expenditure-dashboard"
def post(u,b,t=None):
    h={"Content-Type":"application/json"}
    if t:h["Authorization"]="Bearer "+t
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(u,data=json.dumps(b).encode(),headers=h,method="POST"),timeout=25).read() or b"{}"), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:200]}"
tok=(post(IDN+"/realms/%s/login"%TENANT,{"username":"steward"})[0] or {}).get("access_token")
if not tok: tok=(post(IDN+"/realms/%s/login"%TENANT,{"username":"janaagraha-dev","password":"Airawat@123"})[0] or {}).get("access_token")
print("writer token:", bool(tok))

def m(ref, kind, title, writes=None, reads=None, layer="domain"):
    return {"ref":ref,"kind":kind,"title":title,"origin":"new","status":"draft","layer":layer,
            "writes":writes or [],"reads":reads or []}
members=[
 # --- Data In (drivers) ---
 m("ds:janaagraha:driver/works-register-extractor","driver","Works Register Extractor (Work Order PDFs)",
   writes=["works.work_order","works.contract","works.contractor","works.payment"], reads=[]),
 m("ds:janaagraha:driver/road-matcher","driver","Road Matcher (OSM → road/segment)",
   writes=["geo.road","geo.road_segment"], reads=["geo.osm_feature"]),
 m("ds:janaagraha:driver/ka-eprocurement","driver","Karnataka eProcurement (tenders)",
   writes=["procurement.tender"], reads=["works.work_order"]),
 # --- Logic & AI (engine) ---
 m("ds:janaagraha:engine/ped-reconciler","engine","PED Reconciler (link + flag overpay/DLP)",
   writes=["works.work_order"], reads=["works.work_order","works.contract","works.payment","procurement.tender","geo.road_segment"], layer="intelligence"),
 # --- Apps & Screens ---
 m("ds:janaagraha:app/ped-explorer","app","PED Explorer (citizen map → road drill)",
   reads=["geo.road","geo.road_segment","works.work_order","works.contract","works.contractor","works.payment","procurement.tender","knowledge.document","civic.issue"],
   writes=["civic.issue"], layer="interaction"),
 m("ds:janaagraha:app/ped-ingest-console","app","PED Ingest Console (run & review the extractor)",
   reads=["works.work_order","procurement.tender"], writes=[], layer="interaction"),
]
proj={"schema":"studio.project","id":PID,"tenant":TENANT,
      "title":"Public Expenditure Dashboard",
      "goal":"Track public money to the neighbourhood level — pick a road, see every public work on it (cost, contractor, tender, payment, defect-liability, documents). Spec: ds:janaagraha:service_spec/public-expenditure-dashboard",
      "brief":{"users":["civic.resident","civic.rwa","civic.ward-committee-member","works.engineer"],
               "entities":["geo.road","works.work_order","procurement.tender","works.contract","works.payment"],
               "data_sources":["BBMP work register (Work Order PDFs)","Karnataka eProcurement","OpenStreetMap roads"],
               "decisions":["which works touch a road?","overpaid vs contract?","still under contractor liability?"],
               "triggers":["new work-order PDFs extracted","tender portal refresh"]},
      "service_spec":"ds:janaagraha:service_spec/public-expenditure-dashboard",
      "status":"active","members":members}
_,we=post(KERNEL+"/v1/registry/objects",{"id":PID,"object":proj},tok)
print("write project:", "OK" if not we else "ERR "+str(we))
if we and "service_spec" in str(we):     # schema may reject the extra field → drop + retry
    proj.pop("service_spec",None)
    _,we=post(KERNEL+"/v1/registry/objects",{"id":PID,"object":proj},tok)
    print("  retry without service_spec:", "OK" if not we else "ERR "+str(we))
# verify
r,_=post(KERNEL+"/v1/registry/resolve",{"id":PID},tok)
o=((r.get("results") or [{}])[0].get("object") or {}) if r else {}
mm=o.get("members") or []
print("  project:", o.get("title"), "| members:", len(mm))
from collections import Counter
print("  by layer:", dict(Counter(x.get("layer") for x in mm)))
for x in mm: print("   -", x.get("kind"), x.get("ref").split("/")[-1], "writes",x.get("writes"))
