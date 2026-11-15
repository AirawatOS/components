import json, math, os
H = []
def case(key, title, tags, body, schema, fields, note=""):
    H.append({"key": key, "title": title, "tags": tags, "body": body.strip(), "schema": schema, "fields": fields, "note": note})
case("police-table-distractors", "Police stations from a table with distractor rows", ["table","distractor","ceiling","multi-step"],
"""URDPFI Table 8.58, norms for police facilities (population served per unit, land):
| Category | Population per unit | Land |
| Police post | 40,000-50,000 (only where no police station serves the area) | 0.16 ha |
| Police station | 90,000 | 1.50 ha, plus 0.05 ha for civil defence and home guards |
| District office and battalion | 10 lakh | 4.80 ha |
| District jail | 10 lakh | 10.00 ha |
Each requirement is per population served "or part thereof".
A ward of 3,10,000 people is fully served by police stations and has 2 today, each already with its civil-defence land.
How many new police stations are needed, and how much land in total for them?""",
'{"new_stations": integer, "land_ha": number}',
{"new_stations": math.ceil(310000/90000)-2, "land_ha": round((math.ceil(310000/90000)-2)*1.55,2)})
need=math.ceil(1160000/15000); gap=need-51
case("conflicting-population", "Conflicting population figures", ["conflict","recency","ceiling","cost"],
"""Census 2011 gives the ward's population as 8,42,000. A 2024 household survey by the city gives 11.6 lakh. The plan must use the most recent figure.
URDPFI norm: one neighbourhood park of 1 ha for every 15,000 population or part thereof. The ward has 51 neighbourhood parks.
Developing a park costs Rs 1.2 crore per hectare.
How many parks does the ward need, what is the gap, and what would closing it cost (Rs crore)?""",
'{"parks_needed": integer, "gap": integer, "cost_crore": number}',
{"parks_needed": need, "gap": gap, "cost_crore": round(gap*1.2,2)})
p=950000*1.03**10*1.02**10; dem=p*135/1e6; sup=dem*1.15
case("two-stage-projection", "Two-stage growth with losses", ["projection","compound","multi-step","units"],
"""A city has 9.5 lakh people in 2026. Population grows 3% a year (compound) until 2036, then 2% a year (compound) until 2046.
Design for 135 litres per person per day, and add 15% of that demand for distribution losses.
What is the 2046 population, the 2046 demand without losses (MLD), and the supply to plan for including losses (MLD)?""",
'{"population_2046": number, "demand_mld": number, "supply_mld": number}',
{"population_2046": round(p), "demand_mld": round(dem,1), "supply_mld": round(sup,1)})
case("mixed-terrain-density", "Density for a town on plain and hill land", ["range","split","condition"],
"""URDPFI developed-area density for medium towns (1 to 5 lakh people): 100-150 persons per hectare in plain areas, 60-90 in hill areas.
A medium town of 1,80,000 people will house 60% of them on plain land and 40% on hill land.
What is the smallest and largest total developed area (ha) the density ranges allow?""",
'{"min_area_ha": number, "max_area_ha": number}',
{"min_area_ha": round(108000/150+72000/90,1), "max_area_ha": round(108000/100+72000/60,1)})
case("m1-restricted", "Mixed industrial zone: permitted, restricted or prohibited", ["zoning","threshold","classify"],
"""URDPFI zoning for an M1 (mixed industrial) zone: non-industrial uses (commercial, institutional, recreational, residential) may take up to 20-30% of the area as of right; with the Competent Authority's approval ("restricted") they may be increased to between 20 and 50%; above 50% is not allowed.
A 12 ha M1 parcel proposes 4.2 ha of commercial and residential use.
What is the non-industrial share (%), and is the proposal permitted as of right, restricted (needs approval), or prohibited?""",
'{"share_pct": number, "status": "permitted" | "restricted" | "prohibited"}',
{"share_pct": 35.0, "status": "restricted"})
case("landfill-screening", "Screening landfill sites against three rules", ["multi-constraint","prohibition","abstain"],
"""Rules for a new solid waste landfill:
1. Prohibited within 10 km of an Aerodrome Reference Point (ARP).
2. Not on land with slope above 45 degrees (undevelopable).
3. Not inside an eco-sensitive zone, which here extends 10 km around the protected area.
Candidate sites:
- Site A: 7 km from the ARP, slope 10 degrees, 25 km from the protected area.
- Site B: 12 km from the ARP, slope 48 degrees, 30 km from the protected area.
- Site C: 14 km from the ARP, slope 20 degrees, 6 km from the protected area.
Which sites are acceptable? If none is, say "none".""",
'{"acceptable": "A" | "B" | "C" | "none", "count": integer}',
{"acceptable": "none", "count": 0}, "every site fails one rule; tests that the model does not pick the 'least bad' site")
budget=10; cost=1.2; n=int(budget//cost); short={"C":5,"B":4,"A":3}; alloc={}
for w in ["C","B","A"]:
    k=min(short[w],n); alloc[w]=k; n-=k
case("budget-priority", "Allocating a park budget by need", ["allocation","greedy","crore"],
"""Park shortfalls (number of 1 ha neighbourhood parks): Ward A 3, Ward B 4, Ward C 5.
Developing a park costs Rs 1.2 crore. The budget is Rs 10 crore, and only whole parks can be funded.
Fund the ward with the largest shortfall first, completing its shortfall before moving to the next.
How many parks does each ward get, and how much money is left over (Rs crore)?""",
'{"parks_a": integer, "parks_b": integer, "parks_c": integer, "leftover_crore": number}',
{"parks_a": alloc["A"], "parks_b": alloc["B"], "parks_c": alloc["C"], "leftover_crore": round(budget-cost*sum(alloc.values()),2)})
cnt=math.ceil(2300000/100000); sqm=cnt*5000
case("dhobi-acres", "Dhobi ghats for a city, in acres", ["lakh","units","ceiling"],
"""URDPFI norm: one dhobi ghat for every 1 lakh population or part thereof, each with a minimum of 5,000 sqm.
A city has 23 lakh people. 1 ha = 10,000 sqm; 1 acre = 0.404686 ha.
How many dhobi ghats are needed, and what is their total minimum area in acres?""",
'{"count": integer, "area_acres": number}',
{"count": cnt, "area_acres": round(sqm/10000/0.404686,2)})
case("reverse-capacity", "Largest population a park area supports", ["inverse","ceiling"],
"""URDPFI norm: one neighbourhood park of 1 ha for every 15,000 population or part thereof.
A new colony has land for exactly 7 neighbourhood parks.
What is the largest population the colony can have while still meeting the norm?""",
'{"max_population": integer}',
{"max_population": 7*15000}, "'or part thereof': 105,001 people would need an eighth park")
p=192000*1.04**7
case("grown-ward", "Ward grown since a survey", ["projection","compound","ceiling","gap"],
"""In 2019 a ward had 1,92,000 people and 2 police stations. It has grown 4% a year (compound) since.
URDPFI norm: one police station for every 90,000 population or part thereof.
What is the 2026 population, and how many more police stations does the ward need in 2026?""",
'{"population_2026": number, "additional_stations": integer}',
{"population_2026": round(p), "additional_stations": math.ceil(p/90000)-2})
here=os.path.dirname(os.path.abspath(__file__))
json.dump(H, open(os.path.join(here,"hard.json"),"w"), indent=1)
for c in H: print(c["key"], c["fields"])
