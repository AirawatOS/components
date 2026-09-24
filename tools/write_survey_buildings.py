import json, time, math, urllib.request, urllib.error
import h3
IDN="http://airawat-identity:8093"; KERNEL="http://kernel:8080"; TENANT="amritsar"
def login(r,u):
    q=urllib.request.Request(f"{IDN}/realms/{r}/login",data=json.dumps({"username":u}).encode(),headers={"Content-Type":"application/json"},method="POST")
    return json.loads(urllib.request.urlopen(q,timeout=15).read())["access_token"]
tok=login(TENANT,TENANT+"-admin")
def put(oid,obj):
    q=urllib.request.Request(KERNEL+"/v1/registry/objects",data=json.dumps({"id":oid,"object":obj}).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+tok},method="POST")
    try: urllib.request.urlopen(q,timeout=30).read(); return None
    except urllib.error.HTTPError as e: return f"{e.code} {e.read().decode()[:160]}"
recs=json.load(open("/tmp/survey_buildings.json"))
NOW=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
def area_m2(geom):
    ring=geom["coordinates"][0]; a=0.0
    for i in range(len(ring)-1): a+=ring[i][0]*ring[i+1][1]-ring[i+1][0]*ring[i][1]
    return abs(a)/2*(111320**2)*math.cos(math.radians(ring[0][1]))
wrote=0; unreg=0; err=None
for r in recs:
    lon,lat=r["centroid"]; hc=h3.latlng_to_cell(lat,lon,9)
    oid=f"ds:{TENANT}:building/{hc}/tax-{r['uid']}"
    obj={"schema":"geo.building","id":oid,"tenant":TENANT,"geometry":r["geometry"],
         "centroid":[round(lon,7),round(lat,7)],"h3_id":hc,"resolution":9,"layer":"L1","levels":1,
         "height_m":9.0,"area_m2":round(area_m2(r["geometry"]),1),"confidence":0.95,
         "method":"tax-"+r["tax_status"],                       # status carrier City Scan will colour by
         "source":{"aoi":f"ds:{TENANT}:space/aoi/amritsar","asset":"property-survey","driver":"ingest-property-survey"},
         "source_ref":"ds:airawat:ref/source/property-survey","observed_at":NOW,
         "prov":{"method":"survey-footprint","observed_at":NOW,"derived_from":["property-survey-shp"]}}
    e=put(oid,obj)
    if e: err=(oid,e); break
    wrote+=1; unreg+=1 if r["tax_status"]=="unregistered" else 0
print(f"wrote {wrote} geo.building, {unreg} unregistered (status in method: tax-unregistered/tax-registered)")
if err: print("ERR:", err)
# report the h3 cells the unregistered buildings landed in (matches the choropleth hotspots)
from collections import Counter
c=Counter(h3.latlng_to_cell(r["centroid"][1],r["centroid"][0],9) for r in recs if r["tax_status"]=="unregistered")
print("top unregistered cells:", c.most_common(3))
