"""Tax Gap Finder — govern the `tax` + `util` domains and the analytical-spine schemas.

THE IDEA. Find properties that are under-taxed or unassessed by triangulating INDEPENDENT signals against a cadastral
master: a tax ledger (geocoded from addresses), utility connections (electricity/water), building footprints, and
satellite change detection. Every record is fuzzily linked across datasets and spatially joined to a parcel; mismatches
(a utility connection or a building where there is NO tax assessment; a change detected where the assessment is stale)
become scored `tax.candidate`s for field verification. ALL thresholds + fuzzy weights live in `tax.match_policy` — a
tenant-editable config the engines read, never hardcode.
"""
import json, urllib.request, urllib.error, http.cookiejar
APP="http://localhost:8099"; NET="http://localhost:8090"; IDN="http://localhost:8093"
ID_PAT=r"^ds:[a-z0-9-]+:\S+$"
def post(url, body, token=None, cj=None):
    req=urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}, method="POST")
    if token: req.add_header("Authorization","Bearer "+token)
    op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)) if cj is not None else urllib.request.build_opener()
    try: return json.loads(op.open(req,timeout=30).read()), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:240]}"
STOK=post(IDN+"/realms/airawat/login",{"username":"steward"})[0]["access_token"]
cj=http.cookiejar.CookieJar(); post(APP+"/login",{"realm":"airawatdeveloper","username":"devrick"},cj=cj)
S=lambda:{"type":"string"}; Sn=lambda:{"type":["string","null"]}; N=lambda:{"type":"number"}; Nn=lambda:{"type":["number","null"]}
B=lambda:{"type":["boolean","null"]}; O=lambda:{"type":"object"}; On=lambda:{"type":["object","null"]}; A=lambda:{"type":"array","items":{"type":"object"}}
def defn(sid,kind,reqd,props,desc):
    p={"schema":{"const":sid},"id":{"type":"string","pattern":ID_PAT}}; p.update(props)
    return {"type":"object","x-kind":kind,"additionalProperties":False,"required":["schema","id"]+reqd,"properties":p,"title":sid,"description":desc}

for dom,label in [("tax","Taxation & Revenue"),("util","Utilities")]:
    r,err=post(NET+"/v1/domains",{"domain":dom,"label":label},token=STOK)
    print(f"domain {dom}:", "OK" if r else ("(exists?) "+str(err)))

SCHEMAS={
 # THE CONFIG BACKBONE — every threshold + fuzzy weight lives here (tenant-editable via the Tax Config Console). The
 # engines READ this; nothing is hardcoded. Groups are open objects so the policy can evolve without a schema bump.
 "tax.match_policy": ("record",["name"],{
    "name":S(),"status":{"enum":["active","draft"]},
    "geocode":O(),          # {floor: 0.5, bands:{high:0.85, medium:0.65, low:0.4}} — min acceptable + high/med/low bands
    "address_parse":O(),    # {high:{min_tokens,requires:[..]}, medium:{min_tokens}, low:{min_tokens}} — parse quality tiers
    "geometry_hygiene":O(), # {on_boundary_tolerance_m:5, single_parcel:"accept", on_boundary:"flag", multi_parcel:"flag", outside_all:"flag"}
    "fuzzy_weights":O(),    # {mobile:0.4, name:0.3, address:0.3} — record-linkage field weights (sum ~1)
    "fuzzy_thresholds":O(), # {match:0.85, review:0.6} — >=match auto-link, >=review = candidate, else no-link
    "candidate":O(),        # {min_confidence:0.5, signal_weights:{utility_no_tax, footprint_no_assessment, change_detected, under_assessed}}
    "tenant":Sn(),"updated_by":Sn(),"updated_at":Sn(),"created_at":Sn()},
    "The tax-gap MATCHING POLICY: geocode confidence bands, address-parse tiers, geometric-hygiene rules, and fuzzy record-linkage weights + thresholds. Tenant-editable; the engines read it, never hardcode."),
 # THE LEDGER ROW — a property-tax assessment. address_string is the geocode SOURCE (ledgers rarely carry coordinates).
 "tax.assessment": ("entity",["uid"],{
    "uid":S(),"owner":Sn(),"mobile":Sn(),"address_string":Sn(),"ward":Sn(),
    "assessed_value":Nn(),"annual_tax":Nn(),"paid_status":{"enum":["paid","partial","unpaid","unknown"]},
    "point":On(),"geocode_confidence":Nn(),"geocode_band":Sn(),"address_parse_quality":Sn(),  # geocoded from address_string
    "parcel_id":Sn(),"geometry_hygiene":Sn(),                                                  # after spatial join
    "tenant":Sn(),"source":Sn(),"created_at":Sn()},
    "A PROPERTY-TAX ASSESSMENT (ledger row). Geocoded from address_string → point + band; spatially joined → parcel_id."),
 # THE CADASTRAL MASTER — the parcels the other layers join against. geometry = the plot polygon; bbox for fast join.
 "geo.parcel": ("entity",["parcel_id"],{
    "parcel_id":S(),"land_use":Sn(),"ward":Sn(),"area_sqm":Nn(),
    "geometry":On(),"bbox":{"type":["array","null"],"items":{"type":"number"}},   # [minlon,minlat,maxlon,maxlat]
    "owner":Sn(),"tenant":Sn(),"source":Sn(),"created_at":Sn()},
    "A CADASTRAL PARCEL (the GIS master): plot polygon + land use. The layer every other signal is spatially joined to."),
 # UTILITY PROXY — an electricity/water connection. Independent evidence a property is OCCUPIED/BUILT. May carry a point.
 "util.connection": ("entity",["uid","utility_type"],{
    "uid":S(),"utility_type":{"enum":["electricity","water","sewer","gas"]},
    "account_no":Sn(),"consumer_name":Sn(),"mobile":Sn(),"address_string":Sn(),"tier":Sn(),
    "point":On(),"geocode_confidence":Nn(),"geocode_band":Sn(),"parcel_id":Sn(),
    "tenant":Sn(),"source":Sn(),"created_at":Sn()},
    "A UTILITY CONNECTION (electricity/water/…): a proxy that a property is occupied. Uses its own lat/long if present, else geocoded from address_string, then joined to a parcel."),
 # CHANGE-DETECTION HIT — a satellite-derived signal (AlphaEarth embedding delta) that a parcel changed between epochs.
 "tax.change_signal": ("event",["change_type","score"],{
    "parcel_id":Sn(),"cell":Sn(),"point":On(),
    "change_type":{"enum":["new_build","expanded","land_use_change","demolition"]},
    "score":N(),"from_epoch":Sn(),"to_epoch":Sn(),
    "observed_at":Sn(),"source":Sn(),"tenant":Sn(),"created_at":Sn()},
    "A CHANGE-DETECTION SIGNAL: a satellite (AlphaEarth embedding) delta indicating new/expanded/changed built form on a parcel between two epochs, with a magnitude score."),
 # THE FLAG — a scored tax-gap candidate for field verification. evidence[] pins which signals fired; confidence ∈ [0,1].
 "tax.candidate": ("record",["status","confidence"],{
    "parcel_id":Sn(),"point":On(),"ward":Sn(),
    "evidence":A(),         # [{signal, detail, weight}] — utility_no_tax | footprint_no_assessment | change_detected | under_assessed
    "confidence":N(),"estimated_revenue":Nn(),"recommended_action":Sn(),
    "matched_assessment":Sn(),"matched_utility":Sn(),
    "status":{"enum":["flagged","assigned","verifying","verified","dismissed"]},
    "recommendation":Sn(),  # a governance.recommendation (AI flags, humans decide — ADR-0029)
    "tenant":Sn(),"created_at":Sn()},
    "A TAX-GAP CANDIDATE: a scored, evidence-backed suspicion that a parcel is unassessed or under-assessed. Flagged for field verification — never an automatic decision."),
}
for sid,(kind,reqd,props,desc) in SCHEMAS.items():
    d=defn(sid,kind,reqd,props,desc)
    pr,pe=post(APP+"/bridge/schema-request",{"schema_id":sid,"version":"0.1.0","definition":d,"x_kind":kind,"justification":"Tax Gap Finder: config-driven property-tax enforcement (spatial join + change detection + scored candidates)."},cj=cj)
    if pe: print(f"  {sid}: PROPOSE {pe}"); continue
    dr,de=post(NET+"/v1/schema-requests/decide",{"schema_id":sid,"version":"0.1.0","decision":"approve"},token=STOK)
    print(f"  {sid}: {'OK '+dr['published_schema'] if dr else 'ERR '+str(de)}")
