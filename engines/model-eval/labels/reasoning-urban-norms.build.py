import json, math
C = []
def case(key, title, tags, prompt, fields, note=""):
    C.append({"key": key, "title": title, "tags": tags, "prompt": prompt.strip(), "fields": fields, "note": note})
J = "\n\nAnswer with ONLY a JSON object with exactly these fields: "
# 1 facility count with "or part thereof"
pop=235000; n=math.ceil(pop/90000)
case("police-stations", "Police stations for a ward", ["count","ceiling"],
 f"URDPFI norm: one police station for every 90,000 population or part thereof; each needs 1.50 ha of land.\nA ward has a population of 2,35,000.\nHow many police stations does it need, and how much land in total?"+J+'{"stations": integer, "land_ha": number}',
 {"stations": n, "land_ha": round(n*1.5,2)})
# 2 gap
case("police-gap", "Police station gap", ["gap","ceiling"],
 "URDPFI norm: one police station for every 90,000 population or part thereof; each needs 1.50 ha.\nA ward of 2,35,000 people has 2 police stations today.\nHow many more stations are needed, and how much additional land?"+J+'{"additional_stations": integer, "additional_land_ha": number}',
 {"additional_stations": n-2, "additional_land_ha": round((n-2)*1.5,2)})
# 3 lakh conversion
p=42e5; k=math.ceil(p/5e5)
case("district-parks", "District parks for a city", ["lakh","ceiling"],
 "URDPFI norm (plain areas): one district park of 25 ha for every 5 lakh population or part thereof.\nA city has a population of 42 lakh.\nHow many district parks does it need and what total area?"+J+'{"parks": integer, "area_ha": number}',
 {"parks": k, "area_ha": k*25})
# 4 classify + density range
p=320000
case("medium-town-area", "Developed area from density", ["classify","range","division"],
 "Settlement classes: small town under 1 lakh; medium town 1 lakh to 5 lakh; large city 5 to 10 lakh; metropolitan 10 lakh to 1 crore; megapolis above 1 crore.\nURDPFI developed-area density (persons per hectare, plain areas): small 75-125, medium 100-150, large 125-175, metropolitan 125-175, megapolis above 200.\nA town in a plain area will have 3,20,000 people.\nWhich class is it, and what is the smallest and largest developed area (ha) the density range allows?"+J+'{"class": "small" | "medium" | "large" | "metropolitan" | "megapolis", "min_area_ha": number, "max_area_ha": number}',
 {"class": "medium", "min_area_ha": round(p/150,1), "max_area_ha": round(p/100,1)})
# 5 hill variant
p=80000
case("hill-town-area", "Hill town developed area", ["classify","range","condition"],
 "Settlement classes: small town under 1 lakh; medium town 1 lakh to 5 lakh; large city 5 to 10 lakh.\nURDPFI developed-area density (persons per hectare): small towns 75-125 in plain areas and 45-75 in hill areas; medium towns 100-150 plain and 60-90 hill.\nA hill town will have 80,000 people.\nWhat is the smallest and largest developed area (ha) the density range allows?"+J+'{"min_area_ha": number, "max_area_ha": number}',
 {"min_area_ha": round(p/75,1), "max_area_ha": round(p/45,1)})
# 6 unit conversion
need=100*0.404686
case("township-acres", "Township minimum area in acres", ["units","threshold"],
 "Maharashtra's integrated township policy requires a minimum area of 100 acres (1 acre = 0.404686 ha).\nA developer holds 38 ha.\nDoes the land meet the minimum, and how many more hectares would be needed (0 if none)?"+J+'{"meets_minimum": true | false, "shortfall_ha": number}',
 {"meets_minimum": False, "shortfall_ha": round(need-38,2)})
# 7 projection + design period + demand
p0=12e5; yrs=30; p=p0*1.025**yrs
case("water-design-demand", "Water demand at design year", ["projection","compound","units"],
 "CPHEEO design period for a water distribution system: 30 years from commissioning.\nA distribution system is commissioned in 2026 for a city of 12 lakh people, growing at 2.5% a year (compound).\nDesign for 135 litres per person per day.\nWhat is the design year, the design-year population, and the demand in million litres per day (MLD)?"+J+'{"design_year": integer, "population": number, "demand_mld": number}',
 {"design_year": 2056, "population": round(p), "demand_mld": round(p*135/1e6,1)})
# 8 mixed use compliance
tot=2.4; res=1.3; share=res/tot*100; conv=0.6*tot-res
case("m2-compliance", "Mixed residential zone compliance", ["percentage","threshold","inverse"],
 "URDPFI zoning: in an M2 (mixed residential) zone, residential use must be at least 60% of the area.\nA 2.4 ha M2 parcel has 1.3 ha residential, 0.8 ha commercial and 0.3 ha institutional.\nWhat is the residential share (%), does it comply, and how many hectares of non-residential use must convert to residential to comply (0 if none), keeping the parcel size fixed?"+J+'{"residential_share_pct": number, "complies": true | false, "convert_ha": number}',
 {"residential_share_pct": round(share,1), "complies": False, "convert_ha": round(conv,2)})
# 9 prohibition radius
case("tannery-airport", "Tannery near an airport", ["prohibition","distance"],
 "URDPFI zoning: butcheries, tanneries and solid waste disposal sites are prohibited within 10 km of an Aerodrome Reference Point (ARP).\nA tannery is proposed 8.5 km from the ARP.\nIs it allowed, and how much farther (km) would it have to be to be allowed (0 if already allowed)?"+J+'{"allowed": true | false, "move_further_km": number}',
 {"allowed": False, "move_further_km": 1.5})
# 10 slope partition
parts=[(4.2,30),(1.6,47),(0.9,52),(2.3,44)]; dev=sum(a for a,s in parts if s<=45)
case("slope-developable", "Developable area by slope", ["filter","sum","threshold"],
 "URDPFI: land with slope higher than 45 degrees is undevelopable.\nA site has four parts: 4.2 ha at 30 degrees, 1.6 ha at 47 degrees, 0.9 ha at 52 degrees, and 2.3 ha at 44 degrees.\nHow much of the site is developable, and what share of the whole site is that?"+J+'{"developable_ha": number, "developable_pct": number}',
 {"developable_ha": round(dev,2), "developable_pct": round(dev/sum(a for a,_ in parts)*100,1)})
# 11 insufficient information
case("insufficient-fire", "Missing norm — fire stations", ["insufficient","abstain"],
 "URDPFI norms given to you: one police station per 90,000 population (1.50 ha); one dhobi ghat per 1 lakh population (5,000 sqm).\nA ward has 2,35,000 people.\nUsing ONLY the norms given above, how many fire stations does it need? If the norms given do not allow an answer, say so rather than guessing."+J+'{"answer": "insufficient_information" | integer}',
 {"answer": "insufficient_information"}, "tests whether the model refuses to invent a missing norm")
# 12 conflict: stricter state rule
p=250000
case("open-space-conflict", "URDPFI vs a stricter state rule", ["conflict","precedence","rate"],
 "URDPFI suggests open space of 1.4 to 1.6 ha per 1,000 persons for medium towns. The state's development control rules require 2.0 ha per 1,000 persons, and state rules prevail where they are stricter.\nA medium town has 2,50,000 people.\nHow much open space is required, and which source decides it?"+J+'{"required_ha": number, "source": "urdpfi" | "state"}',
 {"required_ha": 2.0*p/1000, "source": "state"})
# 13 multi-facility land budget
p=150000
items={"police":(90000,1.5),"dhobi":(100000,0.5),"park":(100000,5),"ground":(100000,2)}
cnt={k:math.ceil(p/v[0]) for k,v in items.items()}; land=sum(cnt[k]*items[k][1] for k in items)
case("sector-land-budget", "Land budget for a new sector", ["multi-step","ceiling","sum"],
 "URDPFI norms, each per population served 'or part thereof':\n- police station: 1 per 90,000; 1.50 ha each\n- dhobi ghat: 1 per 1 lakh; 5,000 sqm each\n- community park: 1 per 1 lakh; 5 ha each\n- community multipurpose ground: 1 per 1 lakh; 2 ha each\nA new sector is planned for 1,50,000 people.\nHow many police stations does it need, and how much land in total for all four facilities (ha)?"+J+'{"police_stations": integer, "total_land_ha": number}',
 {"police_stations": cnt["police"], "total_land_ha": round(land,2)})
# 14 ranking by deficit
W={"A":(98000,4),"B":(142000,6),"C":(61000,0)}
d={w:math.ceil(p_/15000)-e for w,(p_,e) in W.items()}; top=max(d,key=d.get)
case("ward-park-priority", "Which ward is most short of parks", ["rank","ceiling","gap"],
 "URDPFI norm: one neighbourhood park (1 ha) for every 15,000 population or part thereof.\nWard A: 98,000 people, 4 neighbourhood parks.\nWard B: 1,42,000 people, 6 parks.\nWard C: 61,000 people, 0 parks.\nWhich ward has the largest shortfall in number of parks, and what is each ward's shortfall?"+J+'{"top_ward": "A" | "B" | "C", "shortfall_a": integer, "shortfall_b": integer, "shortfall_c": integer}',
 {"top_ward": top, "shortfall_a": d["A"], "shortfall_b": d["B"], "shortfall_c": d["C"]})
# 15 budget in crore
case("park-budget", "Park budget in crore", ["crore","division","gap"],
 "Developing a park costs Rs 1.2 crore per hectare. The city has a budget of Rs 15 crore and a park deficit of 14 ha.\nHow many hectares can the budget develop, and how much more money (Rs crore) is needed to close the whole deficit?"+J+'{"fundable_ha": number, "extra_crore": number}',
 {"fundable_ha": round(15/1.2,2), "extra_crore": round(14*1.2-15,2)})
json.dump(C, open(__file__.rsplit("/",1)[0]+"/cases.json","w"), indent=1)
for c in C: print(c["key"], c["fields"])
