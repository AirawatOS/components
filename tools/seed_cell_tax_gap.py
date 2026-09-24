#!/usr/bin/env python3
"""Make the tax gap RENDER in City Scan as a top-level choropleth: govern cell.tax_gap (measure) then roll the
surveyed / unregistered counts onto the SAME H3 grid the map already draws. We snap each parcel to the nearest
cell.buildings cell (amritsar has 3194, each with h3_id + centroid + boundary) and copy that cell_ref — so the
tax-gap cells line up exactly with the existing built-up grid. Run in app-host. Env: IDN NET KERNEL TENANT."""
import os, json, time, urllib.request, urllib.error
IDN=os.environ.get("IDN","http://airawat-identity:8093"); NET=os.environ.get("NET","http://airawat-network:8090")
KERNEL=os.environ.get("KERNEL","http://kernel:8080"); TENANT=os.environ.get("TENANT","amritsar")
def login(r,u):
    q=urllib.request.Request(f"{IDN}/realms/{r}/login",data=json.dumps({"username":u}).encode(),headers={"Content-Type":"application/json"},method="POST")
    return json.loads(urllib.request.urlopen(q,timeout=15).read())["access_token"]
def post(url,body,tok):
    h={"Content-Type":"application/json"}
    if tok: h["Authorization"]="Bearer "+tok
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(url,data=json.dumps(body).encode(),headers=h,method="POST"),timeout=120).read() or b"{}"),None
    except urllib.error.HTTPError as e: return None,f"{e.code} {e.read().decode()[:200]}"

stok=login("airawat","steward")
# 1) GOVERN cell.tax_gap (idempotent — ignore 'already exists')
ID_PAT=r"^ds:[a-z0-9-]+:\S+$"
defn={"type":"object","x-kind":"measure","x-measures-of":"geo.parcel","additionalProperties":False,"title":"cell.tax_gap",
  "required":["schema","id","observed_at","prov"],
  "properties":{"schema":{"const":"cell.tax_gap"},"id":{"type":"string","pattern":ID_PAT},
    "space":{"type":"object"},"surveyed_count":{"type":["number","null"]},"unregistered_count":{"type":["number","null"]},
    "ward":{"type":["string","null"]},"observed_at":{"type":"string"},"prov":{"type":"object"},
    "tenant":{"type":["string","null"]},"source":{"type":["string","null"]},"created_at":{"type":["string","null"]}},
  "description":"Property-tax gap rolled up per H3 cell: surveyed built properties in the cell and how many are UNREGISTERED (in the survey, not the tax register). City Scan choropleth."}
r,e=post(NET+"/v1/schema-requests",{"schema_id":"cell.tax_gap","version":"0.1.0","definition":defn,"x_kind":"measure","justification":"Tax Gap Finder per-cell unregistered density."},stok)
print("schema-request:", e or "created")
r,e=post(NET+"/v1/schema-requests/decide",{"schema_id":"cell.tax_gap","version":"0.1.0","decision":"approve"},stok)
print("schema-decide:", e or "OK")

# 2) ROLL UP onto the existing cell.buildings grid
atok=login(TENANT,TENANT+"-admin")
def resolve_all(schema,want,limit=100000):
    r,e=post(KERNEL+"/v1/registry/resolve",{"filter":{"schema":schema},"limit":limit},atok)
    if e: print(schema,"resolve err",e); return []
    out=[]
    for x in (r.get("results") or r.get("objects") or []):
        o=x.get("object") or x
        if o.get("id","").startswith(f"ds:{TENANT}:"):
            v=want(o)
            if v: out.append(v)
    return out
def cellref(o):
    cr=o.get("cell_ref") or o.get("space") or {}
    return (cr.get("centroid"),cr) if cr.get("centroid") and cr.get("h3_id") else None
grid_cells=resolve_all("cell.buildings", cellref)
print("grid cells (cell.buildings):", len(grid_cells))
def pcen(o):
    g=o.get("geometry") or {}
    if g.get("type")=="Point" and g.get("coordinates"): return (o.get("parcel_id"),g["coordinates"])
    p=o.get("point") or {}
    if p.get("lon") is not None: return (o.get("parcel_id"),[p["lon"],p["lat"]])
    return None
parcels=resolve_all("geo.parcel", pcen)                       # (bare parcel_id, [lon,lat])
cands=set(resolve_all("tax.candidate", lambda o:o.get("parcel_id")))   # candidate.parcel_id is the SAME bare AMR-* key
print("parcels:",len(parcels)," candidate parcels:",len(cands))
def nearest(lon,lat):                                          # brute-force nearest of 3194 cells (fast, exact)
    best=None; bd=1e9
    for cen,cr in grid_cells:
        d=(cen[0]-lon)**2+(cen[1]-lat)**2
        if d<bd: bd=d; best=cr
    return best
agg={}
for pid,coords in parcels:
    cr=nearest(coords[0],coords[1])
    if not cr: continue
    h=cr["h3_id"]; a=agg.setdefault(h,{"s":0,"u":0,"cr":cr}); a["s"]+=1
    if pid in cands: a["u"]+=1
NOW=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
batch=[]
for h,a in agg.items():
    cr=a["cr"]; cid=f"ds:{TENANT}:cell/tax_gap/{h}"
    batch.append({"scope":"cell.tax_gap","id":cid,"object":{"schema":"cell.tax_gap","id":cid,"tenant":TENANT,
        "space":{"h3":h,"h3_id":h,"res":9,"centroid":cr.get("centroid"),"boundary":cr.get("boundary")},
        "surveyed_count":a["s"],"unregistered_count":a["u"],"observed_at":NOW,
        "prov":{"component":"component://ds:airawatdeveloper:engine/tax-gap-engine","at":NOW,"method":"h3-rollup"},
        "source":"tax-gap-engine","created_at":NOW}})
matched=sum(a["s"] for a in agg.values()); unreg=sum(a["u"] for a in agg.values())
wrote=0
for it in batch:                                              # admin writes go via /v1/registry/objects (Bearer), not /v1/write/batch (run-token)
    r,e=post(KERNEL+"/v1/registry/objects",{"id":it["id"],"object":it["object"]},atok)
    if e: print("  write err",it["id"],e); break
    wrote+=1
print(f"cells written={wrote}/{len(batch)} (matched {matched}/{len(parcels)} parcels, {unreg} unregistered)")
if batch: print("sample:", {k:batch[0]['object'][k] for k in ('id','surveyed_count','unregistered_count')})
