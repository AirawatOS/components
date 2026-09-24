#!/usr/bin/env python3
"""Make the tax gap RENDER in City Scan as a top-level choropleth: govern cell.tax_gap (measure) then roll the
surveyed / unregistered counts up into H3 cells. We BORROW the h3_id of the nearest geo.building (amritsar already
has 55k, each with a res-9 h3_id) — no h3 lib needed here; deck.gl draws the hexagon from the id. Run in app-host.
Env: IDN NET KERNEL TENANT."""
import os, json, time, math, urllib.request, urllib.error
IDN=os.environ.get("IDN","http://airawat-identity:8093"); NET=os.environ.get("NET","http://airawat-network:8090")
KERNEL=os.environ.get("KERNEL","http://kernel:8080"); TENANT=os.environ.get("TENANT","amritsar")
def login(r,u):
    q=urllib.request.Request(f"{IDN}/realms/{r}/login",data=json.dumps({"username":u}).encode(),headers={"Content-Type":"application/json"},method="POST")
    return json.loads(urllib.request.urlopen(q,timeout=15).read())["access_token"]
def post(url,body,tok):
    h={"Content-Type":"application/json"};
    if tok: h["Authorization"]="Bearer "+tok
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(url,data=json.dumps(body).encode(),headers=h,method="POST"),timeout=120).read() or b"{}"),None
    except urllib.error.HTTPError as e: return None,f"{e.code} {e.read().decode()[:200]}"

stok=login("airawat","steward")
# 1) GOVERN cell.tax_gap (measure). space block {h3,res,centroid} = the cell_ref City Scan reads.
ID_PAT=r"^ds:[a-z0-9-]+:\S+$"
defn={"type":"object","x-kind":"measure","x-measures-of":"geo.parcel","additionalProperties":False,"title":"cell.tax_gap",
  "required":["schema","id","observed_at","prov"],
  "properties":{"schema":{"const":"cell.tax_gap"},"id":{"type":"string","pattern":ID_PAT},
    "space":{"type":"object"},              # {h3, res, centroid:[lon,lat]}
    "surveyed_count":{"type":["number","null"]},"unregistered_count":{"type":["number","null"]},
    "ward":{"type":["string","null"]},"observed_at":{"type":"string"},"prov":{"type":"object"},
    "tenant":{"type":["string","null"]},"source":{"type":["string","null"]},"created_at":{"type":["string","null"]}},
  "description":"Property-tax gap rolled up per H3 cell: how many surveyed built properties fall in the cell and how many of them are UNREGISTERED (in the survey, not in the tax register). Renders as a City Scan choropleth."}
r,e=post(NET+"/v1/schema-requests",{"schema_id":"cell.tax_gap","version":"0.1.0","definition":defn,"x_kind":"measure",
     "justification":"Tax Gap Finder: per-cell unregistered-property density for the City Scan map."},stok)
print("schema-request:", e or "created")
r,e=post(NET+"/v1/schema-requests/decide",{"schema_id":"cell.tax_gap","version":"0.1.0","decision":"approve"},stok)
print("schema-decide:", e or ("OK "+str((r or {}).get("published_schema") or r)))

# 2) ROLL UP. Read buildings (h3), parcels, candidates as tenant admin; borrow nearest building's h3.
atok=login(TENANT,TENANT+"-admin")
def resolve_all(schema, want):
    out=[]; cursor=None
    while True:
        body={"filter":{"schema":schema},"limit":10000}
        if cursor: body["cursor"]=cursor
        r,e=post(KERNEL+"/v1/registry/resolve",body,atok)
        if e: print(schema,"resolve err",e); break
        rows=r.get("results") or r.get("objects") or []
        for x in rows:
            o=x.get("object") or x
            if o.get("id","").startswith(f"ds:{TENANT}:"): out.append(want(o))
        cursor=r.get("cursor") or r.get("next_cursor")
        if not cursor or not rows: break
    return [o for o in out if o]
blds=resolve_all("geo.building", lambda o:(o.get("centroid"),o.get("h3_id")) if o.get("centroid") and o.get("h3_id") else None)
print("buildings loaded:", len(blds))
def pcen(o):
    g=o.get("geometry") or {};
    if g.get("type")=="Point" and g.get("coordinates"): return (o.get("id"),o["geometry"]["coordinates"])
    p=o.get("point") or {};
    if p.get("lon") is not None: return (o.get("id"),[p["lon"],p["lat"]])
    return None
parcels=resolve_all("geo.parcel", pcen)
cands=set(resolve_all("tax.candidate", lambda o:o.get("parcel_id")))
print("parcels:",len(parcels)," candidates:",len(cands))
# grid index of buildings by 0.003deg (~300m) bins
grid={}
for cen,h in blds:
    k=(round(cen[0],3),round(cen[1],3)); grid.setdefault(k,[]).append((cen,h))
def nearest_h3(lon,lat):
    best=None; bd=1e9
    for dx in (-0.003,0,0.003):
        for dy in (-0.003,0,0.003):
            for cen,h in grid.get((round(lon+dx,3),round(lat+dy,3)),[]):
                d=(cen[0]-lon)**2+(cen[1]-lat)**2
                if d<bd: bd=d; best=h
    return best
agg={}
for pid,coords in parcels:
    lon,lat=coords[0],coords[1]; h=nearest_h3(lon,lat)
    if not h: continue
    a=agg.setdefault(h,{"s":0,"u":0,"pts":[]}); a["s"]+=1; a["pts"].append([lon,lat])
    if pid in cands: a["u"]+=1
NOW=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
batch=[]
for h,a in agg.items():
    cen=[round(sum(p[0] for p in a["pts"])/len(a["pts"]),6), round(sum(p[1] for p in a["pts"])/len(a["pts"]),6)]
    cid=f"ds:{TENANT}:cell/tax_gap/{h}"
    batch.append({"scope":"cell.tax_gap","id":cid,"object":{"schema":"cell.tax_gap","id":cid,"tenant":TENANT,
        "space":{"h3":h,"res":9,"centroid":cen},"surveyed_count":a["s"],"unregistered_count":a["u"],
        "observed_at":NOW,"prov":{"component":"component://ds:airawatdeveloper:engine/tax-gap-engine","at":NOW,"method":"h3-rollup"},
        "source":"tax-gap-engine","created_at":NOW}})
w,e=post(KERNEL+"/v1/write/batch",{"items":batch,"asserted_at":NOW},atok)
print(f"cells={len(batch)}  write:", e or f"written {(w or {}).get('written')}")
print("sample:", batch[0]["object"] if batch else None)
