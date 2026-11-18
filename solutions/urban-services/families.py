#!/usr/bin/env python3
"""Urban service FAMILIES (templates) and the showcase services built from them — one source for both:
  python3 families.py            → services/<service_id>.json for every showcase service + families.json (the Studio's
                                    'New from a family' catalogue, embedded into Service Studio by build_studio_families.py)
A family is a function returning a v2 offering (the exact shape Service Studio round-trips). Services fill in the
department, title, fee rule code, roles and certificate; everything is ordinary data the Studio can then edit."""
import json, os

ESC = "urban.commissioner"
JUR = {"city-da": "demo-city", "city-mc": "demo-city", "uit": "demo-town", "housing-board": "demo-state", "sidc": "demo-state", "state-udd": "demo-state"}
PROP_HIDDEN = ["property_id", "zone", "address", "land_use", "area_sqm"]


def _schema(fields):
    props, order, req = {}, [], []
    for k, p, required in fields:
        props[k] = p; order.append(k)
        if required: req.append(k)
        if p.get("x-lookup") == "urban.property":
            for h in PROP_HIDDEN:
                props[h] = {"type": "string", "x-from": k + "." + h}; order.append(h)
    return {"type": "object", "required": req, "properties": props, "x-order": order}


def _offer(tenant, sid, title, desc, fields, workflow, docs=(), fee=None, pay=True, lookup=False, creds=("credential.identity_verified",)):
    sla = {w["step"]: w.pop("sla") for w in workflow}
    ful = {**({"fee_service": fee} if fee else {}), **({"payment": "before_submit"} if fee and pay else {}),
           "documents": [{"type": d, "when": w} for d, w in docs], "sla_hours": sla, "escalate_to": ESC, "workflow": workflow}
    if not ful["documents"]: ful.pop("documents")
    o = {"tenant": tenant, "service_id": sid, "domain": "urban", "title": title, "description": desc,
         "interaction": ["search"] + (["select"] if fee else []) + ["init", "status"], "jurisdiction": JUR[tenant],
         "audience": "civic.resident", "solution": "ds:airawatdeveloper:solution/declarative-service",
         "required_credentials": list(creds), "scope_map": {"area": "zone"} if lookup else {}, "fulfillment": ful,
         "request_schema": _schema(fields)}
    if fee: o["fee_ref"] = "urban.fee_schedule"
    o["sla_ref"] = ", ".join("%s %dh" % (k, v) for k, v in sla.items())
    return o


S = lambda title, **kw: {"type": "string", "title": title, **kw}
N = lambda title, **kw: {"type": "number", "title": title, **kw}
PROP = ("property", S("Unified Property ID", **{"x-section": "Property", "x-lookup": "urban.property",
        "description": "Printed on your allotment letter, lease deed or tax receipt."}), True)
REMARKS = ("remarks", S("Anything the office should know", **{"x-widget": "textarea", "x-section": "Other"}), False)


def step(step, role, task, sla, office=False, issues=None, effects=None, inspection=None):
    w = {"step": step, "role": role, **({"office_by": "zone"} if office else {}), "task": task}
    if issues: w["issues"] = issues
    if inspection: w["inspection"] = {"checklist": inspection}
    if effects == "transfer_holding":
        w["effects"] = [{"close": {"schema": "urban.property_holding", "match": {"property": "$form.property"}, "patch": {"until": "$today"}}},
                        {"write": {"schema": "urban.property_holding", "property": "$form.property", "holder_name": "$form.transferee_name",
                                   "holder_ref": "$applicant", "relation": "owner", "share_pct": 100, "since": "$today", "source_doc": "$case"}}]
    if effects == "convert_freehold":
        w["effects"] = [{"close": {"schema": "urban.property", "match": {"id": "$form.property"}, "patch": {"tenure": "freehold"}}},
                        {"close": {"schema": "urban.lease", "match": {"property": "$form.property", "status": "active"}, "patch": {"status": "converted"}}}]
    w["sla"] = sla
    return w


# ---------------- the six families ----------------
def mutation(t, sid, title, fee, certificate=None, holder="property holder"):
    return _offer(t, sid, title, "Record a %s in the new holder's name after a sale, inheritance, gift or family settlement. Look the property up by "
                  "its Unified Property ID; the application goes to the zone office of the property." % holder, [
        PROP,
        ("transfer_reason", S("Reason for transfer", **{"x-section": "Transfer", "enum": ["sale", "inheritance", "gift", "family-settlement"]}), True),
        ("transferor_name", S("Present holder (as recorded)"), True),
        ("relation_to_transferor", S("Your relation to the deceased holder", enum=["spouse", "son", "daughter", "parent", "other-legal-heir"],
                                     **{"x-show-if": {"transfer_reason": "inheritance"}}), False),
        ("transferee_name", S("New holder (your name)", **{"x-prefill": "identity.name", "description": "As on your verified identity."}), True),
        ("deed_no", S("Registered deed number", **{"x-show-if": {"transfer_reason": ["sale", "gift", "family-settlement"]}}), False),
        ("deed_date", S("Deed date", format="date", **{"x-show-if": {"transfer_reason": ["sale", "gift", "family-settlement"]}}), False),
        REMARKS],
        [step("scrutiny", "urban.dealing-assistant", "Check the application, the documents and the dues. Forward, return for documents, or reject.", 72, office=True),
         step("approval", "urban.estate-officer", "Approve the transfer. The holder record is updated and the order is issued.", 48,
              issues=certificate, effects="transfer_holding")],
        docs=[("ownership-proof", {}), ("sale-deed", {"transfer_reason": "sale"}), ("death-certificate", {"transfer_reason": "inheritance"}),
              ("succession-certificate", {"transfer_reason": "inheritance"}), ("affidavit", {"transfer_reason": ["gift", "family-settlement"]}), ("no-dues", {})],
        fee=fee, lookup=True)


def conversion(t, sid, title, fee):
    return _offer(t, sid, title, "Convert a leasehold property to freehold. Look the property up, give the reason and attach the lease deed and "
                  "no-dues; the conversion charge is paid online. On approval the property becomes freehold.", [
        PROP, ("conversion_reason", S("Reason for conversion", **{"x-widget": "textarea", "x-section": "Conversion"}), True), REMARKS],
        [step("scrutiny", "urban.dealing-assistant", "Check the lease, dues and documents.", 72, office=True),
         step("approval", "urban.estate-officer", "Approve the conversion; the order is issued and the property becomes freehold.", 48,
              issues="credential.urban_freehold_conversion", effects="convert_freehold")],
        docs=[("lease-deed", {}), ("no-dues", {})], fee=fee, lookup=True)


def land_use(t, sid, title, fee, desc):
    return _offer(t, sid, title, desc, [
        ("plot_location", S("Plot location (village / sector, khasra or plot number)", **{"x-section": "Land"}), True),
        ("area_sqm", N("Plot area (sq m)"), True),
        ("current_land_use", S("Current land use", enum=["agricultural", "residential", "commercial", "industrial", "institutional"]), True),
        ("land_use", S("Proposed land use", enum=["residential", "commercial", "industrial", "institutional", "mixed"]), True),
        ("purpose", S("Purpose of the change", **{"x-widget": "textarea"}), True), REMARKS],
        [step("scrutiny", "urban.assistant-town-planner", "Check conformity with the master plan, zoning and the documents.", 120),
         step("site-inspection", "urban.field-inspector", "Inspect the plot.", 72,
              inspection=["Plot exists at the stated location", "Present use matches the application", "Access road is available",
                          "No encroachment on public land"]),
         step("approval", "urban.town-planner", "Approve the change of land use; the order is issued.", 72, issues="credential.urban_land_use_change")],
        docs=[("ownership-proof", {}), ("site-plan", {}), ("affidavit", {})], fee=fee)


def permission(t, sid, title, fee, certificate, desc, extra_fields, checklist, approver="urban.town-planner", inspector="urban.junior-engineer"):
    return _offer(t, sid, title, desc, [PROP] + extra_fields + [REMARKS],
        [step("scrutiny", "urban.assistant-town-planner", "Scrutinise the plans against the bye-laws and the master plan.", 72, office=True),
         step("site-inspection", inspector, "Inspect the site against the application.", 72, office=True, inspection=checklist),
         step("approval", approver, "Approve the permission with its conditions; it is issued to the applicant.", 48, issues=certificate)],
        docs=[("ownership-proof", {}), ("site-plan", {}), ("building-plan", {}), ("structural-certificate", {}), ("no-dues", {})], fee=fee, lookup=True)


def licence(t, sid, title, fee, renewal=False):
    fields = [("trade_name", S("Business / trade name", **{"x-section": "Business"}), True),
              ("trade_type", S("Type of trade", enum=["shop", "restaurant", "eatery", "workshop", "warehouse", "clinic", "salon", "other"]), True),
              ("premises_address", S("Premises address"), True),
              ("area_sqm", N("Premises area (sq m)", description="The fee depends on the premises area."), True),
              ("premises_ownership", S("The premises are", enum=["owned", "rented"]), True)]
    if renewal:
        fields.insert(0, ("licence_no", S("Current licence number", **{"x-section": "Licence"}), True))
    steps = [step("verification", "urban.dealing-assistant", "Verify the application and documents.", 48)]
    if not renewal:
        steps.append(step("inspection", "urban.health-officer", "Inspect the premises.", 72,
                          inspection=["Premises match the application", "Clean, with waste segregation", "Drinking water and toilet available",
                                      "Fire extinguisher present (eateries, warehouses)", "No obstruction of the public way"]))
    steps.append(step("approval", "urban.health-officer" if renewal else "urban.approving-authority", "Approve; the licence is issued.", 24,
                      issues="credential.urban_trade_licence"))
    return _offer(t, sid, title, ("Renew a trade licence before it expires. " if renewal else "Get a licence to run a trade at a premises in the city. ")
                  + "The fee depends on the premises area and is paid online.", fields + [REMARKS], steps,
        docs=[("identity-proof", {}), ("trade-premises-proof", {}), ("noc-fire", {"trade_type": ["restaurant", "eatery", "warehouse"]})], fee=fee)


def certificate(t, sid, title, desc, issues, approver, fee=None, inspection=None, inspector=None, lookup=True, extra=(), docs=()):
    steps = [step("verification", "urban.dealing-assistant" if approver != "urban.fire-officer" else "urban.fire-officer",
                  "Verify the application, the records and the dues.", 48, office=lookup)]
    if inspection:
        steps.append(step("inspection", inspector, "Inspect the premises.", 72, inspection=inspection))
    steps.append(step("issue", approver, "Approve; the certificate is issued.", 24, issues=issues))
    return _offer(t, sid, title, desc, ([PROP] if lookup else []) + list(extra) + [REMARKS], steps, docs=list(docs), fee=fee, lookup=lookup)


def allotment(t, sid, title, fee, schemes):
    return _offer(t, sid, title, "Register for the allotment of a house or plot by lottery. Choose the scheme and your income category; "
                  "the registration fee is paid online.", [
        ("scheme", S("Scheme", **{"x-section": "Scheme"}, enum=[s for s, _ in schemes], **{"x-enum-labels": dict(schemes)}), True),
        ("category", S("Income category", enum=["ews", "lig", "mig", "hig"], **{"x-enum-labels": {"ews": "EWS", "lig": "LIG", "mig": "MIG", "hig": "HIG"}}), True),
        ("annual_income", N("Annual household income (₹)", **{"x-section": "Household"}), True),
        ("family_size", N("Family members"), True),
        ("reserved_category", S("Reserved category (if any)", enum=["none", "sc", "st", "obc", "disabled", "ex-servicemen", "government-employee"]), False),
        REMARKS],
        [step("verification", "urban.dealing-assistant", "Verify eligibility, income and documents.", 120),
         step("registration", "urban.estate-officer", "Approve the registration for the lottery.", 48, issues="credential.urban_allotment_registration")],
        docs=[("identity-proof", {}), ("address-proof", {}), ("income-certificate", {"category": ["ews", "lig"]}),
              ("caste-category-certificate", {"reserved_category": ["sc", "st", "obc"]})], fee=fee)


def booking(t, sid, title, fee):
    return _offer(t, sid, title, "Book a community centre for a function. The charge is per day and paid online.", [
        ("centre", S("Community centre", **{"x-section": "Booking"}, enum=["zone-1-centre", "zone-2-centre", "zone-3-centre", "zone-4-centre"],
                     **{"x-enum-labels": {"zone-1-centre": "Zone 1 community centre", "zone-2-centre": "Zone 2 community centre",
                                          "zone-3-centre": "Zone 3 community centre", "zone-4-centre": "Zone 4 community centre"}}), True),
        ("from_date", S("From", format="date"), True), ("days", N("Number of days"), True),
        ("purpose", S("Purpose", enum=["wedding", "religious", "social", "meeting", "other"]), True), REMARKS],
        [step("approval", "urban.revenue-officer", "Check availability and approve the booking.", 24)], fee=fee)


FAMILIES = {
  "mutation": ("Mutation / name transfer", "Change the recorded holder of a property after a sale, inheritance or gift; updates the department's record.",
               lambda: mutation("city-da", "new-name-transfer", "Name transfer", "name-transfer", "credential.urban_name_transfer")),
  "conversion": ("Conversion (leasehold → freehold)", "Convert tenure; on approval the property becomes freehold and the lease is marked converted.",
                 lambda: conversion("city-da", "new-freehold-conversion", "Freehold conversion", "freehold-conversion")),
  "permission": ("Permission with site inspection", "Plans scrutinised, the site inspected with a checklist, then approved with conditions (building permission, layout, road cutting).",
                 lambda: permission("city-da", "new-building-permission", "Building permission", "building-permission-500", "credential.urban_building_permission",
                                    "Get permission to build on your plot.", [("built_up_sqm", N("Proposed built-up area (sq m)", **{"x-section": "Proposal"}), True),
                                    ("floors", N("Number of floors"), True)], ["Plot boundaries match the site plan", "Setbacks as per the plan", "Road width as stated"])),
  "licence": ("Licence", "Verification, premises inspection and approval; the licence is issued with its validity (trade, health, signage).",
              lambda: licence("city-mc", "new-trade-licence", "Trade licence", "trade-licence")),
  "certificate": ("Certificate / NOC", "Records and dues verified, optionally an inspection, then the certificate is issued (no-dues, NOC, patta).",
                  lambda: certificate("city-da", "new-no-dues", "No-dues certificate", "Get a certificate that nothing is payable on your property.",
                                      "credential.urban_no_dues", "urban.revenue-officer")),
  "allotment": ("Allotment / lottery registration", "Register for a scheme lottery by income category; eligibility verified, registration issued.",
                lambda: allotment("housing-board", "new-allotment", "Allotment registration", "allotment-processing", [("hb-phase-1", "Housing Scheme Phase I")])),
}

SERVICES = [
  # City Development Authority (name transfer + freehold already exist)
  permission("city-da", "city-da-building-permission", "Building permission (up to 500 sq m)", "building-permission-500", "credential.urban_building_permission",
             "Get permission to build on your plot (up to 500 sq m). The plans are scrutinised, the site is inspected, and the permission is issued "
             "with its conditions. The fee is calculated from the plot area.",
             [("built_up_sqm", N("Proposed built-up area (sq m)", **{"x-section": "Proposal"}), True), ("floors", N("Number of floors"), True),
              ("building_use", S("Use of the building", enum=["residential", "commercial", "mixed"]), True),
              ("architect", S("Registered architect (name and registration no.)"), True)],
             ["Plot boundaries match the site plan", "Setbacks as per the plan", "Road width as stated", "No existing construction beyond the plan",
              "No encroachment on public land"]),
  permission("city-da", "city-da-sub-division", "Sub-division / layout of a plot", "sub-division", "credential.urban_layout_approval",
             "Divide a plot into smaller plots. The layout is scrutinised, the site inspected and the layout approved.",
             [("proposed_plots", N("Number of plots proposed", **{"x-section": "Proposal"}), True), ("layout_purpose", S("Purpose", enum=["sale", "family-partition", "development"]), True)],
             ["Plot boundaries match the records", "Internal access road provided", "Minimum plot sizes respected"]),
  certificate("city-da", "city-da-lease-deed", "Lease deed issue", "Get the lease deed for an allotted property. The fee is paid online.",
              "credential.urban_lease_deed", "urban.estate-officer", fee="lease-deed", docs=[("allotment-letter", {}), ("possession-letter", {}), ("no-dues", {})]),
  certificate("city-da", "city-da-no-dues", "No-dues certificate", "Get a certificate that nothing is payable to the Authority on your property.",
              "credential.urban_no_dues", "urban.revenue-officer", docs=[("tax-receipt", {})]),
  # City Municipal Corporation
  licence("city-mc", "city-mc-trade-licence", "Trade licence", "trade-licence"),
  licence("city-mc", "city-mc-trade-licence-renewal", "Trade licence renewal", "trade-licence", renewal=True),
  permission("city-mc", "city-mc-building-permission", "Building permission (up to 500 sq m)", "building-permission-500", "credential.urban_building_permission",
             "Get permission to build on your plot in the city (up to 500 sq m). Scrutiny, a site inspection, then the permission with conditions.",
             [("built_up_sqm", N("Proposed built-up area (sq m)", **{"x-section": "Proposal"}), True), ("floors", N("Number of floors"), True),
              ("architect", S("Registered architect (name and registration no.)"), True)],
             ["Plot boundaries match the site plan", "Setbacks as per the plan", "Road width as stated"]),
  certificate("city-mc", "city-mc-fire-noc", "Fire safety NOC", "Get the fire department's no-objection certificate for a building or business. "
              "The premises are inspected for fire safety.", "credential.urban_noc", "urban.fire-officer", lookup=False,
              inspection=["Fire extinguishers installed and serviced", "Exits clear and marked", "Fire alarm / detectors working",
                          "Electrical wiring safe", "Water storage for firefighting available"], inspector="urban.fire-officer",
              extra=[("premises_address", S("Premises address", **{"x-section": "Premises"}), True),
                     ("occupancy", S("Use of the premises", enum=["shop", "restaurant", "hotel", "hospital", "school", "warehouse", "office", "residential-high-rise"]), True),
                     ("floor_area_sqm", N("Floor area (sq m)"), True)], docs=[("building-plan", {}), ("ownership-proof", {})]),
  booking("city-mc", "city-mc-community-centre", "Community centre booking", "community-centre-booking"),
  # Urban Improvement Trust
  certificate("uit", "uit-patta", "Patta (lease deed) issue", "Get the patta for a property in a Trust scheme. The fee is paid online.",
              "credential.urban_lease_deed", "urban.estate-officer", fee="lease-deed", docs=[("allotment-letter", {}), ("no-dues", {})]),
  mutation("uit", "uit-name-transfer", "Name transfer (mutation) of a property", "name-transfer", "credential.urban_name_transfer"),
  permission("uit", "uit-sub-division", "Sub-division of a plot", "sub-division", "credential.urban_layout_approval",
             "Divide a plot in a Trust scheme into smaller plots.", [("proposed_plots", N("Number of plots proposed", **{"x-section": "Proposal"}), True)],
             ["Plot boundaries match the records", "Access to every new plot"]),
  # State Housing Board
  allotment("housing-board", "hb-allotment-registration", "Allotment registration (lottery)", "allotment-processing",
            [("hb-phase-1", "Housing Scheme Phase I"), ("hb-phase-2", "Housing Scheme Phase II"), ("hb-affordable", "Affordable Housing Scheme")]),
  mutation("housing-board", "hb-name-transfer", "Name transfer of an allotted house", "name-transfer", "credential.urban_name_transfer", holder="Board house"),
  # Industrial Development Corporation
  _offer("sidc", "sidc-plot-transfer", "Transfer of an industrial plot", "Transfer an industrial plot to another enterprise. The transfer charge is a "
         "percentage of the declared value, paid online.", [
           PROP, ("transferee_enterprise", S("Transferee enterprise", **{"x-section": "Transfer"}), True),
           ("transferee_name", S("Applicant (your name)", **{"x-prefill": "identity.name"}), True),
           ("declared_value", N("Declared value of the transfer (₹)"), True),
           ("activity", S("Industrial activity of the transferee", **{"x-widget": "textarea"}), True), REMARKS],
         [step("scrutiny", "urban.dealing-assistant", "Check the lease conditions, dues and documents.", 72, office=True),
          step("approval", "urban.estate-officer", "Approve the transfer; the holder record is updated.", 48, issues="credential.urban_name_transfer",
               effects="transfer_holding")],
         docs=[("lease-deed", {}), ("no-dues", {}), ("affidavit", {})], fee="industrial-plot-transfer", lookup=True),
  permission("sidc", "sidc-sub-division", "Sub-division of an industrial plot", "sub-division", "credential.urban_layout_approval",
             "Divide an industrial plot.", [("proposed_plots", N("Number of plots proposed", **{"x-section": "Proposal"}), True)],
             ["Plot boundaries match the records", "Each new plot has road access", "Minimum industrial plot size respected"],
             approver="urban.executive-engineer", inspector="urban.junior-engineer"),
  # Urban Development Department (state)
  land_use("state-udd", "udd-land-use-change", "Change of land use", "land-use-change",
           "Apply to change the permitted land use of a plot. The plan is scrutinised, the plot inspected, and the order issued. The charge depends on the proposed use."),
  land_use("state-udd", "udd-90a-conversion", "Conversion of agricultural land (90-A)", "90a-conversion",
           "Convert agricultural land to non-agricultural (residential) use under section 90-A. The charge is per square metre."),
]

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(os.path.join(here, "services"), exist_ok=True)
    for o in SERVICES:
        with open(os.path.join(here, "services", o["service_id"] + ".json"), "w") as f:
            json.dump(o, f, indent=2, ensure_ascii=False); f.write("\n")
    fam = {k: {"label": lab, "about": about, "offering": fn()} for k, (lab, about, fn) in FAMILIES.items()}
    with open(os.path.join(here, "families.json"), "w") as f:
        json.dump(fam, f, indent=1, ensure_ascii=False); f.write("\n")
    by = {}
    for o in SERVICES: by[o["tenant"]] = by.get(o["tenant"], 0) + 1
    print("services written:", len(SERVICES), by, "| families:", list(fam))
