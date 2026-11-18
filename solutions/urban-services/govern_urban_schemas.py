#!/usr/bin/env python3
"""Urban services — the shared master-data vocabulary (registries) every urban department uses.

Governed once in the shared catalogue (domain `urban`), so every department tenant — state department, local-bodies
directorate, housing board, industrial corporation, development authority, improvement trust, municipal corporation —
keeps its records in the SAME shapes, and services, dashboards and the citizen portal work across all of them.
Personal data is kept OUT of the core records: who holds a property, who was allotted a plot and who holds a lease
are separate, `sensitive` records (so the platform keeps them in-house and away from outside AI models).

The GIS parcel itself is `geo.parcel` (already governed); a property links to it rather than duplicating it.
Run where NET/IDN resolve (e.g. the staging app-host container) — the steward files and approves.
"""
import json, os, urllib.error, urllib.request

NET = os.environ.get("NET", "http://network:8090")
IDN = os.environ.get("IDN", "http://identity:8093")
ID = {"type": "string", "pattern": r"^ds:[a-z0-9-]+:\S+$"}
REF = {"type": ["string", "null"], "description": "a record id (ds:<steward>:…)"}
S = lambda d=None: {"type": ["string", "null"], **({"description": d} if d else {})}
N = lambda d=None: {"type": ["number", "null"], **({"description": d} if d else {})}
I = lambda d=None: {"type": ["integer", "null"], **({"description": d} if d else {})}
E = lambda *v: {"enum": list(v) + [None]}
DATE = {"type": ["string", "null"], "description": "YYYY-MM-DD"}
STAMPS = {"tenant": S(), "created_at": S(), "updated_at": S()}
POINT = {"type": ["object", "null"], "additionalProperties": False,
         "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}}, "description": "location (WGS84)"}
LAND_USE = E("residential", "commercial", "industrial", "institutional", "mixed", "public", "recreational", "agricultural")


def obj(sid, kind, subject, desc, required, props):
    return sid, {"type": "object", "title": sid, "x-kind": kind, "x-subject": subject, "description": desc,
                 "additionalProperties": False, "required": ["schema", "id"] + required,
                 "properties": {"schema": {"const": sid}, "id": ID, **props, **STAMPS}}


SCHEMAS = [
 (obj("urban.property", "entity", "urban property", "A property under an urban authority, identified by its UNIFIED PROPERTY ID. "
      "Location, scheme/plot, land use, size and tenure — NOT who holds it (that is urban.property_holding).",
      ["property_id", "authority"],
      {"property_id": {"type": "string", "description": "the Unified Property ID, e.g. UP-CDA-000123"},
       "authority": {"type": "string", "description": "the tenant that administers it"},
       "parcel": REF, "zone": REF, "ward": REF, "scheme": REF, "plot": REF,
       "plot_no": S(), "address": S(), "locality": S(), "pincode": S(),
       "land_use": LAND_USE,
       "property_type": E("plot", "house", "flat", "shop", "office", "industrial-plot", "institutional", "mixed-use"),
       "area_sqm": N("plot / land area"), "built_up_sqm": N(), "floors": I(),
       "tenure": E("leasehold", "freehold", "allotted", "government"),
       "status": E("active", "subdivided", "merged", "cancelled"),
       "location": POINT, "legacy_refs": {"type": ["array", "null"], "items": {"type": "string"}, "description": "ids in older systems"}}),
  "internal"),
 (obj("urban.property_holding", "record", "property holding", "Who holds a property, in what share and capacity, and since when. "
      "Personal data — sensitive.", ["property", "holder_name", "relation"],
      {"property": {"type": "string", "description": "urban.property id"},
       "holder_name": {"type": "string"}, "holder_ref": S("user:// of the holder, when they have an account"),
       "relation": {"enum": ["owner", "co-owner", "lessee", "allottee", "occupant", "heir"]},
       "share_pct": N(), "since": DATE, "until": DATE, "source_doc": S("deed / allotment / order reference")}),
  "sensitive"),
 (obj("urban.scheme", "entity", "urban scheme", "A development, housing or industrial scheme of an authority.",
      ["code", "name", "authority"],
      {"code": {"type": "string"}, "name": {"type": "string"}, "authority": {"type": "string"},
       "kind": E("residential", "housing", "commercial", "industrial", "institutional", "mixed"),
       "area": REF, "total_plots": I(), "launched_on": DATE,
       "status": E("planned", "open", "closed", "completed"), "description": S()}),
  "internal"),
 (obj("urban.plot", "entity", "scheme plot", "A plot in a scheme: category, size, reserve price and availability.",
      ["plot_no", "scheme"],
      {"plot_no": {"type": "string"}, "scheme": {"type": "string", "description": "urban.scheme id"},
       "category": E("EWS", "LIG", "MIG", "HIG", "commercial", "industrial", "institutional"),
       "area_sqm": N(), "reserve_price": N("INR"), "property": REF,
       "status": E("available", "reserved", "allotted", "auction", "blocked"), "location": POINT}),
  "internal"),
 (obj("urban.allotment", "record", "plot allotment", "An allotment of a plot or unit — by lottery, reservation, auction or "
      "an institutional route. Personal data — sensitive.", ["plot", "allottee_name", "mode"],
      {"plot": {"type": "string", "description": "urban.plot id"}, "scheme": REF,
       "allottee_name": {"type": "string"}, "allottee_ref": S("user:// of the allottee, when they have an account"),
       "mode": {"enum": ["lottery", "reservation", "auction", "institutional", "exchange", "direct"]},
       "allotted_on": DATE, "price": N("INR"), "letter_no": S(),
       "payment_status": E("due", "part-paid", "paid", "refunded"),
       "status": E("provisional", "confirmed", "cancelled", "surrendered")}),
  "sensitive"),
 (obj("urban.lease", "record", "lease deed", "A lease deed, patta, freehold conversion or one-time lease certificate on a property. "
      "Personal data — sensitive.", ["property", "lessee_name", "deed_type"],
      {"property": {"type": "string", "description": "urban.property id"},
       "lessee_name": {"type": "string"}, "lessee_ref": S(),
       "deed_type": {"enum": ["lease", "patta", "freehold-conversion", "one-time-lease-certificate", "sub-lease"]},
       "deed_no": S(), "issued_on": DATE, "term_years": I(), "annual_rent": N("INR"),
       "status": E("active", "expired", "converted", "cancelled")}),
  "sensitive"),
 (obj("urban.fee_schedule", "entity", "service fee rule", "A fee rule of an authority for a service: a flat amount, a rate per square "
      "metre, a percentage of value, a per-unit charge or slabs; optional conditions; effective dates; the order it rests on.",
      ["authority", "service", "fee_head", "basis"],
      {"authority": {"type": "string"}, "service": {"type": "string", "description": "the service code it applies to"},
       "fee_head": {"type": "string", "description": "e.g. processing fee, conversion charge, lease rent"},
       "basis": {"enum": ["flat", "per_sqm", "percent_of_value", "per_unit", "slab"]},
       "rate": N(), "unit": S("for per_unit"),
       "slabs": {"type": ["array", "null"], "items": {"type": "object", "additionalProperties": False,
                 "properties": {"upto": {"type": ["number", "null"]}, "rate": {"type": "number"}}}},
       "conditions": {"type": ["object", "null"], "additionalProperties": False, "properties": {
           "land_use": {"type": ["string", "null"]}, "zone": {"type": ["string", "null"]},
           "category": {"type": ["string", "null"]}, "min_area_sqm": {"type": ["number", "null"]},
           "max_area_sqm": {"type": ["number", "null"]}}},
       "effective_from": DATE, "effective_to": DATE, "statutory_ref": S("the order / notification")}),
  "internal"),
 (obj("urban.payment", "record", "service fee payment", "A fee paid for a service application: amount and breakdown "
      "(computed by the department from its fee rules), the payer, the gateway reference, and the application it paid for.",
      ["receipt_no", "service", "amount", "status"],
      {"receipt_no": {"type": "string"}, "service": {"type": "string"}, "authority": S(), "case": REF,
       "amount": {"type": "number"}, "currency": S(), "breakdown": {"type": ["array", "null"], "items": {"type": "object",
       "additionalProperties": False, "properties": {"item": {"type": "string"}, "amount": {"type": "number"},
       "basis": {"type": ["string", "null"]}, "statutory_ref": {"type": ["string", "null"]}}}},
       "payer_ref": S("user:// of the payer"), "property": REF, "method": E("upi", "card", "netbanking", "counter"),
       "gateway": S("payment gateway (sandbox on staging)"), "gateway_ref": S(), "status": {"enum": ["paid", "refunded", "failed"]},
       "paid_at": S()}),
  "internal"),
 (obj("urban.inspection", "record", "field inspection", "A field inspection done as a step of a service application: the "
      "checklist with each item's result, where and when it was done (geo-tag), photos, and the inspector's outcome.",
      ["case", "step", "inspector", "inspected_at", "outcome"],
      {"case": {"type": "string", "description": "platform.case id"}, "service": S(), "step": {"type": "string"},
       "inspector": {"type": "string", "description": "user:// of the inspecting officer"}, "inspected_at": {"type": "string"},
       "location": POINT, "accuracy_m": N(),
       "checklist": {"type": ["array", "null"], "items": {"type": "object", "additionalProperties": False, "properties": {
           "item": {"type": "string"}, "result": {"enum": ["ok", "not-ok", "na"]}, "note": {"type": ["string", "null"]}}}},
       "photos": {"type": ["array", "null"], "items": {"type": "string"}, "description": "filestore file ids"},
       "outcome": {"enum": ["satisfactory", "deficiencies", "not-inspected"]}, "remarks": S()}),
  "internal"),
 (obj("urban.document_type", "entity", "service document type", "A kind of supporting document a service may ask for "
      "(the checklist vocabulary): accepted formats, size limit, who issues it, and how it can be verified.",
      ["code", "name"],
      {"code": {"type": "string"}, "name": {"type": "string"}, "description": S(),
       "formats": {"type": ["array", "null"], "items": {"type": "string"}}, "max_size_mb": N(),
       "issued_by": S(), "verifiable_via": E("none", "digilocker", "esign", "qr", "issuing-authority")}),
  "internal"),
]


# CREDENTIAL TYPES (the claims contract of a signed credential a department issues into a citizen's wallet). Governed
# like any schema; x-kind "fragment" = a value object (the claims), not a stored registry record.
def cred(sid, desc, required, props):
    return sid, {"type": "object", "title": sid, "x-kind": "fragment", "description": desc, "additionalProperties": False,
                 "required": required, "properties": props}


TXT = {"type": "string"}
CREDENTIALS = [
 (("credential.urban_building_permission", {"type": "object", "title": "credential.urban_building_permission", "x-kind": "fragment",
   "description": "Building permission: the plans are approved for construction, with any conditions. Claims are the application's own fields.",
   "x-open": True, "x-open-reason": "the claims are the citizen's application fields, which each department's Studio-built form defines",
   "properties": {"property_id": TXT, "address": TXT, "permission_no": TXT, "valid_till": TXT, "conditions": TXT}}), "internal"),
 (("credential.urban_layout_approval", {"type": "object", "title": "credential.urban_layout_approval", "x-kind": "fragment",
   "description": "Layout / sub-division approval of a plot or scheme. Claims are the application's own fields.",
   "x-open": True, "x-open-reason": "the claims are the citizen's application fields, which each department's Studio-built form defines",
   "properties": {"property_id": TXT, "address": TXT, "approval_no": TXT, "plots": TXT}}), "internal"),
 (("credential.urban_trade_licence", {"type": "object", "title": "credential.urban_trade_licence", "x-kind": "fragment",
   "description": "Trade licence for a business at a premises, with its validity. Claims are the application's own fields.",
   "x-open": True, "x-open-reason": "the claims are the citizen's application fields, which each department's Studio-built form defines",
   "properties": {"property_id": TXT, "address": TXT, "licence_no": TXT, "trade": TXT, "valid_till": TXT}}), "internal"),
 (("credential.urban_noc", {"type": "object", "title": "credential.urban_noc", "x-kind": "fragment",
   "description": "No-objection certificate (e.g. fire safety) for a premises or activity. Claims are the application's own fields.",
   "x-open": True, "x-open-reason": "the claims are the citizen's application fields, which each department's Studio-built form defines",
   "properties": {"property_id": TXT, "address": TXT, "noc_no": TXT, "noc_type": TXT, "valid_till": TXT}}), "internal"),
 (("credential.urban_no_dues", {"type": "object", "title": "credential.urban_no_dues", "x-kind": "fragment",
   "description": "No-dues certificate: nothing is payable to the authority on the property. Claims are the application's own fields.",
   "x-open": True, "x-open-reason": "the claims are the citizen's application fields, which each department's Studio-built form defines",
   "properties": {"property_id": TXT, "address": TXT, "as_on": TXT}}), "internal"),
 (("credential.urban_lease_deed", {"type": "object", "title": "credential.urban_lease_deed", "x-kind": "fragment",
   "description": "Lease deed or patta issued for a property. Claims are the application's own fields.",
   "x-open": True, "x-open-reason": "the claims are the citizen's application fields, which each department's Studio-built form defines",
   "properties": {"property_id": TXT, "address": TXT, "deed_no": TXT, "term_years": TXT}}), "internal"),
 (("credential.urban_allotment_registration", {"type": "object", "title": "credential.urban_allotment_registration", "x-kind": "fragment",
   "description": "Registration for an allotment (lottery) in a scheme, with the category. Claims are the application's own fields.",
   "x-open": True, "x-open-reason": "the claims are the citizen's application fields, which each department's Studio-built form defines",
   "properties": {"property_id": TXT, "address": TXT, "scheme": TXT, "category": TXT, "registration_no": TXT}}), "internal"),
 (("credential.urban_land_use_change", {"type": "object", "title": "credential.urban_land_use_change", "x-kind": "fragment",
   "description": "Order changing the permitted land use of a property. Claims are the application's own fields.",
   "x-open": True, "x-open-reason": "the claims are the citizen's application fields, which each department's Studio-built form defines",
   "properties": {"property_id": TXT, "address": TXT, "from_land_use": TXT, "to_land_use": TXT, "order_no": TXT}}), "internal"),

 (cred("credential.urban_property_holding", "Issued by an urban department when a citizen links its record of a property "
       "they hold to their account (account linking): the property, the capacity and share, and the verified name.",
       ["property_id", "authority", "relation"],
       {"property_id": TXT, "authority": TXT, "relation": TXT, "holder_name": TXT, "address": TXT, "land_use": TXT,
        "area_sqm": {"type": "number"}, "share_pct": {"type": "number"}, "since": TXT}), "internal"),
 (cred("credential.urban_fee_receipt", "Receipt for a service fee paid to an urban department: usable as proof of "
       "payment with the application it was paid for.", ["receipt_no", "service", "amount", "authority"],
       {"receipt_no": TXT, "service": TXT, "service_title": TXT, "authority": TXT, "amount": {"type": "number"}, "currency": TXT,
        "paid_at": TXT, "method": TXT, "gateway": TXT, "property_id": TXT}), "internal"),
 (("credential.urban_freehold_conversion", {"type": "object", "title": "credential.urban_freehold_conversion", "x-kind": "fragment",
   "description": "Freehold conversion order: the property, previously leasehold, is now held freehold. Issued by the department "
                  "that holds the property record. Claims are the application's own fields (they vary by department's form).",
   "x-open": True, "x-open-reason": "the claims are the citizen's application fields, which each department's Studio-built form defines",
   "required": ["property_id"],
   "properties": {"property": TXT, "property_id": TXT, "zone": TXT, "address": TXT, "land_use": TXT, "area_sqm": {"type": ["number", "string"]},
                  "conversion_reason": TXT, "remarks": TXT}}), "internal"),
 (cred("credential.urban_name_transfer", "Name transfer (mutation) order: the property is now recorded in the name of the "
       "transferee. Issued by the department that holds the property record.",
       ["property_id"],
       {"property": TXT, "property_id": TXT, "zone": TXT, "address": TXT, "land_use": TXT, "area_sqm": {"type": ["number", "string"]},
        "transfer_reason": TXT, "transferor_name": TXT, "transferee_name": TXT, "relation_to_transferor": TXT,
        "deed_no": TXT, "deed_date": TXT, "remarks": TXT}), "internal"),
]


def _post(url, body, token=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code} {e.read().decode()[:300]}"


if __name__ == "__main__":
    r, err = _post(IDN + "/login", {"username": "steward"})
    if err:
        raise SystemExit("login failed: " + err.split(" ")[0])
    tok = r["access_token"]
    for (sid, d), sens in SCHEMAS + CREDENTIALS:
        pr, perr = _post(f"{NET}/v1/schema-requests", {"schema_id": sid, "version": "0.1.0", "definition": d, "x_kind": d["x-kind"],
                         "sensitivity": sens, "justification": "Urban services product — shared master data for every urban department."}, tok)
        if perr and "already published" in perr:
            print(f"{sid:28s} already governed"); continue
        dr, derr = _post(f"{NET}/v1/schema-requests/decide", {"schema_id": sid, "version": "0.1.0", "decision": "approve"}, tok)
        print(f"{sid:28s} {sens:9s} " + ("approved" if dr else "FAILED " + str(perr or derr)))
