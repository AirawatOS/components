#!/usr/bin/env python3
"""Urban services — synthetic master data for the seven demo departments (urban.* registries).

Deterministic (seeded) and entirely FICTIONAL: names, schemes, plots, prices and places are generated; locations fall
in a generic city box. Every record is written by the department's own admin (signed in with the password piped on
STDIN — the owner's logins file; never printed) through the kernel, which validates it against the governed schema.
Idempotent: the same run writes the same ids. Run inside a container that reaches identity + kernel (the staging
network container), e.g.:  docker exec -i airawat-network python3 seed_registries.py < ~/urban-demo-logins.txt
"""
import json, os, random, sys, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

IDN = os.environ.get("IDENTITY_URL", "http://identity:8093")
KERNEL = os.environ.get("KERNEL", "http://kernel:8080")
ONLY = [t for t in (os.environ.get("ONLY") or "").split(",") if t]

FIRST = ["Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Krishna", "Ishaan", "Rohan", "Kabir", "Ananya",
         "Diya", "Saanvi", "Aadhya", "Pari", "Myra", "Ira", "Kavya", "Meera", "Neha", "Pooja", "Ritu", "Sunita", "Rakesh",
         "Mahesh", "Suresh", "Dinesh", "Ramesh", "Geeta", "Lata", "Kamla", "Mohan", "Gopal", "Harish", "Naveen", "Farhan",
         "Imran", "Salma", "Zoya", "Harpreet", "Gurpreet", "Simran", "Joseph", "Mary", "Anil", "Sunil", "Rekha", "Usha"]
LAST = ["Sharma", "Verma", "Gupta", "Agarwal", "Jain", "Meena", "Choudhary", "Singh", "Rathore", "Shekhawat", "Joshi",
        "Mathur", "Saxena", "Khandelwal", "Bansal", "Mittal", "Goyal", "Yadav", "Saini", "Kumawat", "Bairwa", "Khan",
        "Qureshi", "Sethi", "Kaur", "Thomas", "Pareek", "Vyas", "Purohit", "Soni"]
DOC_TYPES = [
    ("identity-proof", "Identity proof", "Aadhaar, passport, voter ID or driving licence", "digilocker"),
    ("address-proof", "Address proof", "A recent utility bill, Aadhaar or rent agreement", "digilocker"),
    ("photo", "Passport-size photograph", "Recent photograph of the applicant", "none"),
    ("ownership-proof", "Proof of ownership", "Registered sale deed, lease deed or patta", "issuing-authority"),
    ("sale-deed", "Registered sale deed", "Sale deed registered with the sub-registrar", "issuing-authority"),
    ("lease-deed", "Lease deed / patta", "The lease deed or patta issued by the authority", "issuing-authority"),
    ("allotment-letter", "Allotment letter", "Letter of allotment of the plot or unit", "issuing-authority"),
    ("possession-letter", "Possession letter", "Letter handing over possession", "issuing-authority"),
    ("site-plan", "Site plan", "Site plan showing plot boundaries and dimensions", "none"),
    ("building-plan", "Building plan drawings", "Architectural drawings (DWG / PDF) signed by a registered architect", "none"),
    ("structural-certificate", "Structural stability certificate", "Certificate by a registered structural engineer", "esign"),
    ("noc-fire", "Fire NOC", "No-objection certificate from the fire department", "qr"),
    ("noc-environment", "Environment clearance", "Clearance where the size of the project requires it", "issuing-authority"),
    ("no-dues", "No-dues certificate", "No dues payable to the authority", "qr"),
    ("death-certificate", "Death certificate", "For transfer by inheritance", "digilocker"),
    ("succession-certificate", "Succession certificate / will", "Legal heirship document for transfer by inheritance", "issuing-authority"),
    ("affidavit", "Affidavit", "Notarised affidavit in the prescribed format", "none"),
    ("indemnity-bond", "Indemnity bond", "On stamp paper of the prescribed value", "none"),
    ("tax-receipt", "Latest property-tax receipt", "Receipt of the latest UD / property tax payment", "qr"),
    ("trade-premises-proof", "Proof of trade premises", "Ownership or rent agreement for the premises", "none"),
    ("caste-category-certificate", "Category certificate", "For reserved-category allotments", "digilocker"),
    ("income-certificate", "Income certificate", "For EWS / LIG eligibility", "digilocker"),
]

# Per tenant: (code for property ids, a location box, schemes [(code, name, kind, plots, cat mix)], extra properties, fee rules)
CITY = (26.82, 26.96, 75.72, 75.88)      # a generic city box — fictional names, nothing real is claimed
TOWN = (27.20, 27.26, 75.40, 75.48)
FEES = {
 "state-udd": [("land-use-change", "Conversion charge", "per_sqm", 40, {"land_use": "residential"}, "State land-use change order"),
               ("land-use-change", "Conversion charge", "per_sqm", 80, {"land_use": "commercial"}, "State land-use change order"),
               ("90a-conversion", "90-A conversion charge", "per_sqm", 25, {"land_use": "residential"}, "Land revenue rules — section 90-A")],
 "dlb": [("trade-licence", "Licence fee (model)", "slab", None, None, "Model municipal licensing bye-law"),
         ("building-permission-500", "Building permission fee (model)", "per_sqm", 10, None, "Model building bye-law")],
 "city-da": [("lease-deed", "Lease deed fee", "flat", 1000, None, "Authority fee order"),
             ("freehold-conversion", "Freehold conversion charge", "per_sqm", 150, {"land_use": "residential"}, "Authority fee order"),
             ("freehold-conversion", "Freehold conversion charge", "per_sqm", 300, {"land_use": "commercial"}, "Authority fee order"),
             ("name-transfer", "Name transfer fee", "flat", 2500, None, "Authority fee order"),
             ("sub-division", "Sub-division fee", "per_sqm", 20, None, "Authority fee order"),
             ("building-permission-500", "Building permission fee", "per_sqm", 12, {"max_area_sqm": 500}, "Building bye-law")],
 "city-mc": [("ud-tax", "Urban development tax", "per_sqm", 3.5, {"land_use": "residential"}, "UD tax rules"),
             ("ud-tax", "Urban development tax", "per_sqm", 9, {"land_use": "commercial"}, "UD tax rules"),
             ("trade-licence", "Licence fee", "slab", None, None, "Municipal licensing bye-law"),
             ("building-permission-500", "Building permission fee", "per_sqm", 10, {"max_area_sqm": 500}, "Building bye-law"),
             ("community-centre-booking", "Booking charge per day", "per_unit", 8000, None, "Community centre rates")],
 "uit": [("lease-deed", "Lease deed fee", "flat", 750, None, "Trust fee order"),
         ("name-transfer", "Name transfer fee", "flat", 2000, None, "Trust fee order"),
         ("sub-division", "Sub-division fee", "per_sqm", 15, None, "Trust fee order")],
 "housing-board": [("allotment-processing", "Application processing fee", "flat", 500, None, "Board allotment regulations"),
                   ("name-transfer", "Name transfer fee", "flat", 3000, None, "Board allotment regulations")],
 "sidc": [("industrial-plot-transfer", "Transfer charge", "percent_of_value", 2, None, "Corporation land rules"),
          ("sub-division", "Sub-division fee", "per_sqm", 25, None, "Corporation land rules")],
}
TENANTS = {
 "city-da": ("CDA", CITY, [("green-park", "Green Park Residential Scheme", "residential", 160, "res"),
                           ("sunrise-enclave", "Sunrise Enclave", "residential", 140, "res"),
                           ("central-market", "Central Commercial Complex", "commercial", 60, "com")], 420),
 "city-mc": ("CMC", CITY, [], 1200),
 "uit": ("UIT", TOWN, [("lake-view", "Lake View Scheme", "residential", 100, "res"),
                       ("station-road", "Station Road Scheme", "mixed", 60, "mix")], 160),
 "housing-board": ("HB", CITY, [("hb-phase-1", "Housing Scheme Phase I", "housing", 90, "hous"),
                                ("hb-phase-2", "Housing Scheme Phase II", "housing", 80, "hous"),
                                ("hb-affordable", "Affordable Housing Scheme", "housing", 70, "aff")], 0),
 "sidc": ("SIDC", CITY, [("ia-1-ph1", "Industrial Area 1 — Phase I", "industrial", 60, "ind"),
                         ("ia-2-ph1", "Industrial Area 2 — Phase I", "industrial", 50, "ind"),
                         ("ia-3-ph1", "Industrial Area 3 — Phase I", "industrial", 40, "ind")], 0),
 "state-udd": (None, None, [], 0), "dlb": (None, None, [], 0),
}
CAT = {"res": [("EWS", 40, 60), ("LIG", 60, 90), ("MIG", 120, 180), ("HIG", 200, 300)],
       "com": [("commercial", 50, 150)], "mix": [("MIG", 120, 180), ("commercial", 60, 120)],
       "hous": [("LIG", 45, 65), ("MIG", 80, 110), ("HIG", 120, 160)], "aff": [("EWS", 30, 40), ("LIG", 40, 55)],
       "ind": [("industrial", 500, 4000)]}
RATE = {"EWS": 9000, "LIG": 12000, "MIG": 18000, "HIG": 25000, "commercial": 45000, "industrial": 6000, "institutional": 8000}


def build(tid, rnd):
    code, box, schemes, extra = TENANTS[tid]
    recs = []
    rid = lambda kind, local: "ds:%s:urban/%s/%s" % (tid, kind, local)
    area = lambda c: "ds:%s:area/%s" % (tid, c)
    for c, name, desc, verify in DOC_TYPES:
        recs.append({"schema": "urban.document_type", "id": rid("document_type", c), "code": c, "name": name, "description": desc,
                     "formats": ["pdf", "jpg", "png"] if c != "building-plan" else ["dwg", "pdf"], "max_size_mb": 20 if c == "building-plan" else 5,
                     "verifiable_via": verify})
    for i, (svc, head, basis, rate, cond, ref) in enumerate(FEES.get(tid, [])):
        r = {"schema": "urban.fee_schedule", "id": rid("fee_schedule", "%s-%d" % (svc, i)), "authority": tid, "service": svc,
             "fee_head": head, "basis": basis, "effective_from": "2026-04-01", "statutory_ref": ref}
        if basis == "slab":
            r["slabs"] = [{"upto": 20, "rate": 1500}, {"upto": 50, "rate": 3500}, {"upto": 200, "rate": 7500}, {"upto": None, "rate": 15000}]
            r["unit"] = "premises area (sq m)"
        else:
            r["rate"] = rate
        if basis == "per_unit":
            r["unit"] = "day"
        if cond:
            r["conditions"] = cond
        recs.append(r)
    if not code:
        return recs
    zones = (["zone-%d" % i for i in range(1, 5)] if tid in ("city-da", "city-mc") else ["zone-1", "zone-2"] if tid == "uit"
             else ["circle-1", "circle-2"] if tid == "housing-board" else ["ia-1", "ia-2", "ia-3"])
    loc = lambda: {"lat": round(rnd.uniform(box[0], box[1]), 6), "lon": round(rnd.uniform(box[2], box[3]), 6)}
    person = lambda: rnd.choice(FIRST) + " " + rnd.choice(LAST)
    n = [0]

    def prop(zone, ward=None, scheme=None, plot=None, plot_no=None, land_use="residential", ptype="house", sqm=150.0, tenure="leasehold"):
        n[0] += 1
        upid = "UP-%s-%06d" % (code, n[0])
        p = {"schema": "urban.property", "id": rid("property", upid), "property_id": upid, "authority": tid, "zone": area(zone),
             "land_use": land_use, "property_type": ptype, "area_sqm": sqm, "tenure": tenure, "status": "active", "location": loc(),
             "address": "%s, %s" % (("Plot " + plot_no) if plot_no else ("House %d" % rnd.randint(1, 400)),
                                    scheme[1] if scheme else ("Sector %d" % rnd.randint(1, 30))),
             "legacy_refs": ["OLD/%s/%d" % (code, rnd.randint(10000, 99999))]}
        if ward:
            p["ward"] = area(ward)
        if scheme:
            p["scheme"] = rid("scheme", scheme[0])
        if plot:
            p["plot"] = plot; p["plot_no"] = plot_no
        if ptype != "plot":
            p["built_up_sqm"] = round(sqm * rnd.uniform(0.6, 1.6), 1); p["floors"] = rnd.randint(1, 3)
        recs.append(p)
        return p

    def holders(p, since):
        k = 1 if rnd.random() < 0.8 else 2
        for j in range(k):
            recs.append({"schema": "urban.property_holding", "id": rid("property_holding", "%s-%d" % (p["property_id"], j + 1)),
                         "property": p["id"], "holder_name": person(), "relation": "owner" if k == 1 else "co-owner",
                         "share_pct": 100.0 / k, "since": since})

    for s_code, s_name, kind, nplots, mix in schemes:
        zone = rnd.choice(zones)
        recs.append({"schema": "urban.scheme", "id": rid("scheme", s_code), "code": s_code, "name": s_name, "authority": tid,
                     "kind": kind, "area": area(zone), "total_plots": nplots, "launched_on": "20%02d-%02d-01" % (rnd.randint(10, 24), rnd.randint(1, 12)),
                     "status": "open" if rnd.random() < 0.5 else "closed"})
        for k in range(1, nplots + 1):
            cat, lo, hi = rnd.choice(CAT[mix])
            sqm = round(rnd.uniform(lo, hi), 1)
            plot_no = "%s-%d" % (chr(65 + (k - 1) // 50), k)
            plot_id = rid("plot", "%s-%s" % (s_code, plot_no))
            allotted = rnd.random() < 0.7
            plot = {"schema": "urban.plot", "id": plot_id, "plot_no": plot_no, "scheme": rid("scheme", s_code), "category": cat,
                    "area_sqm": sqm, "reserve_price": round(sqm * RATE[cat], -2), "status": "allotted" if allotted else
                    rnd.choice(["available", "available", "reserved", "auction"]), "location": loc()}
            if allotted:
                lu = "commercial" if cat == "commercial" else "industrial" if cat == "industrial" else "residential"
                p = prop(zone, scheme=(s_code, s_name), plot=plot_id, plot_no=plot_no, land_use=lu,
                         ptype="industrial-plot" if cat == "industrial" else "shop" if cat == "commercial" else rnd.choice(["plot", "house", "house"]),
                         sqm=sqm, tenure="freehold" if rnd.random() < 0.3 else "leasehold")
                plot["property"] = p["id"]
                year = rnd.randint(2008, 2025)
                name = person()
                recs.append({"schema": "urban.allotment", "id": rid("allotment", "%s-%s" % (s_code, plot_no)), "plot": plot_id,
                             "scheme": rid("scheme", s_code), "allottee_name": name,
                             "mode": "auction" if cat in ("commercial",) else "institutional" if rnd.random() < 0.05 else
                             ("reservation" if rnd.random() < 0.15 else "lottery"),
                             "allotted_on": "%d-%02d-%02d" % (year, rnd.randint(1, 12), rnd.randint(1, 28)), "price": plot["reserve_price"],
                             "letter_no": "%s/ALT/%d/%d" % (code, year, k), "payment_status": rnd.choice(["paid", "paid", "paid", "part-paid", "due"]),
                             "status": "confirmed"})
                recs.append({"schema": "urban.property_holding", "id": rid("property_holding", "%s-1" % p["property_id"]),
                             "property": p["id"], "holder_name": name, "relation": "allottee" if p["tenure"] == "leasehold" else "owner",
                             "share_pct": 100.0, "since": "%d-01-01" % year})
                if p["tenure"] == "leasehold" or rnd.random() < 0.3:
                    recs.append({"schema": "urban.lease", "id": rid("lease", p["property_id"]), "property": p["id"], "lessee_name": name,
                                 "deed_type": "patta" if tid in ("uit",) else "lease" if p["tenure"] == "leasehold" else "freehold-conversion",
                                 "deed_no": "%s/LD/%d/%d" % (code, year, k), "issued_on": "%d-%02d-15" % (year, rnd.randint(1, 12)),
                                 "term_years": 99 if p["tenure"] == "leasehold" else None, "annual_rent": round(sqm * 2.5, 0) if p["tenure"] == "leasehold" else None,
                                 "status": "active" if p["tenure"] == "leasehold" else "converted"})
            recs.append(plot)
    for k in range(extra):                                      # properties outside schemes (old city, wards)
        zone = rnd.choice(zones)
        ward = None
        if tid == "city-mc":
            z = int(zone.split("-")[1]); ward = "ward-%02d" % ((z - 1) * 2 + rnd.randint(1, 2))
        lu = rnd.choices(["residential", "commercial", "mixed", "institutional"], [70, 18, 9, 3])[0]
        p = prop(zone, ward=ward, land_use=lu, ptype={"residential": rnd.choice(["house", "house", "flat"]), "commercial": rnd.choice(["shop", "office"]),
                 "mixed": "mixed-use", "institutional": "institutional"}[lu], sqm=round(rnd.uniform(60, 600), 1),
                 tenure=rnd.choice(["freehold", "freehold", "leasehold"]))
        holders(p, "%d-01-01" % rnd.randint(1990, 2025))
    return recs


def call(method, url, body=None, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=h, method=method)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read() or b"{}"), None
    except urllib.error.HTTPError as e:
        return None, "%s %s" % (e.code, (e.read() or b"").decode()[:240])
    except Exception as e:
        return None, str(e)[:200]


def main():
    pw = {}
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-2].endswith("-admin"):
            pw[parts[-2][:-len("-admin")]] = parts[-1]
    for tid in TENANTS:
        if ONLY and tid not in ONLY:
            continue
        if tid not in pw:
            print("%-14s skipped (no login)" % tid); continue
        r, err = call("POST", IDN + "/login", {"username": tid + "-admin", "tenant": tid, "password": pw[tid]})
        tok = (r or {}).get("access_token")
        if not tok:
            print("%-14s login FAILED %s" % (tid, (err or "").split(" ")[0])); continue
        recs = build(tid, random.Random("urban-demo-" + tid))
        for o in recs:
            o["tenant"] = tid
        errors = []

        def put(o):
            r, err = call("POST", KERNEL + "/v1/registry/objects", {"id": o["id"], "object": o}, tok)
            if err:
                errors.append((o["schema"], err))
            return o["schema"]
        with ThreadPoolExecutor(8) as ex:
            done = list(ex.map(put, recs))
        by = {}
        for s in done:
            by[s] = by.get(s, 0) + 1
        bad = {}
        for s, e in errors:
            bad[s] = bad.get(s, 0) + 1
        print("%-14s %s%s" % (tid, ", ".join("%s %d" % (s.split(".")[1], c) for s, c in sorted(by.items())),
                              ("  FAILED: " + ", ".join("%s %d" % (s.split(".")[1], c) for s, c in bad.items())) if bad else ""))
        for s, e in errors[:3]:
            print("   e.g. %s: %s" % (s, e[:200]))


if __name__ == "__main__":
    main()
