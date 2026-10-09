"""ADR-0110 — govern the FULL, AGENCY-NEUTRAL public procurement + public-works lifecycle.

Extends govern_ped_schemas.py: adds the missing lifecycle nouns + bumps the thin ones to a
generic version. Nothing here names an agency/city/state/portal/statute — those are DATA VALUES.

Flow (proven): register domain (steward) -> propose each schema via /bridge/schema-request
(janaagraha-dev session) -> approve via /v1/schema-requests/decide (airawat steward).
Schemas are network-canonical: ds:airawat:schema/<noun>@<ver>.

Run INSIDE a stack container (DNS names identity/network/app resolve there), e.g.:
  docker cp govern_lifecycle_schemas.py airawat-app-host:/tmp/ && \
  docker exec airawat-app-host python3 /tmp/govern_lifecycle_schemas.py
"""
import json, urllib.request, urllib.error
IDN="http://identity:8093"; NET="http://network:8090"; APP="http://127.0.0.1:8099"

def post(u,b,token=None,cookie=None):
    h={"Content-Type":"application/json"}
    if token:h["Authorization"]="Bearer "+token
    if cookie:h["Cookie"]=cookie
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(u,data=json.dumps(b).encode(),headers=h,method="POST"),timeout=30).read() or b"{}"), None
    except urllib.error.HTTPError as e: return None, "%d %s"%(e.code,e.read().decode()[:200])

STOK=(post(IDN+"/realms/airawat/login",{"username":"steward"})[0] or {}).get("access_token")
print("steward token:",bool(STOK))

S=lambda t="string":{"type":t}
NS=lambda:{"type":["string","null"]}; NN=lambda:{"type":["number","null"]}; NI=lambda:{"type":["integer","null"]}
NB=lambda:{"type":["boolean","null"]}; NO=lambda:{"type":["object","null"]}
NAR=lambda it="string":{"type":["array","null"],"items":({"type":it} if isinstance(it,str) else it)}
ID={"type":"string","pattern":"^ds:[a-z0-9-]+:\\S+$"}

def noun(nm, kind, req, props, desc, subject=None):
    p={"id":ID,"schema":{"const":nm},"tenant":NS()}; p.update(props)
    d={"type":"object","title":nm,"x-kind":kind,"required":["schema","id"]+req,
       "properties":p,"additionalProperties":False,"description":desc}
    if subject: d["x-subject"]=subject
    return d

# object-array item shapes (kept generic)
DOC={"type":"object","properties":{"file_name":NS(),"doc_type":NS(),"uuid":NS()},"additionalProperties":True}

SCHEMAS={
 # ---- procurement side (bump to generic @0.2.0: required is tender_ref, not work_order) ----
 "procurement.tender": ("0.2.0", noun("procurement.tender","entity",["tender_ref"],
   {"tender_ref":S(),"source":NS(),"procuring_entity":NS(),"jurisdiction_ref":NS(),
    "title":NS(),"description":NS(),"category":NS(),"sub_category":NS(),"topics":NAR("string"),
    "status":NS(),"invite_type":NS(),"eval_type":NS(),
    "estimated_value":NN(),"accepted_value":NN(),"tender_fee":NN(),"emd":NN(),
    "bid_validity_days":NI(),"no_of_calls":NI(),"contractor_class":NS(),
    "published_at":NS(),"closing_at":NS(),"awarded_at":NS(),"period_year":NS(),
    "bidders":NAR("string"),"num_bidders":NI(),"documents":NAR(DOC),"corrigenda":NAR(DOC),
    "work_code":NS(),"prov":NO(),"confidence":NN(),"ext":NO()},
   "A public procurement notice/award (any portal). tender_ref is the primary key; value/EMD/"
   "dates/bidders/documents from the source feed. Agency/portal are data, not schema.")),

 # ---- works side: bump existing thin nouns (@0.2.0) ----
 "works.work_order": ("0.2.0", noun("works.work_order","entity",["work_order_no"],
   {"work_code":NS(),"job_number":NS(),"work_order_no":S(),"work_order_date":NS(),
    "tender_ref":NS(),"agreement_ref":NS(),"contractor_ref":NS(),
    "work_type":NS(),"other_work":NS(),"work_name":NS(),"significant_landmark":NS(),
    "jurisdiction_ref":NS(),"ward":NS(),"zone":NS(),"budget_ref":NS(),
    "sanctioned_amount":NN(),"accepted_amount":NN(),"premium_pct":NN(),"tax_pct":NN(),
    "sanctioned_year":NS(),"completion_period_days":NI(),"defect_liability_months":NN(),
    "supervising_officer":NS(),"extension_days":NI(),"roads":NAR("string"),
    "n_segments_mapped":NI(),"mapped_len_m":NN(),"status":NS(),"completed_on":NS(),
    "final_cost":NN(),"saving":NN(),"completion_status":NS(),"completion_stated_figure":NN(),
    "prov":NO(),"confidence":NN(),"ext":NO()},
   "The order to execute a public work (any works dept). work_code links the lifecycle; "
   "defect_liability_months + completion_period_days drive citizen accountability.")),

 "works.contractor": ("0.2.0", noun("works.contractor","entity",["name"],
   {"name":S(),"trade_name":NS(),"tax_id":NS(),"reg_id":NS(),"reg_no":NS(),
    "reg_class":NS(),"reg_category":NS(),"licence_valid_from":NS(),"licence_valid_to":NS(),
    "prov":NO()},
   "A contractor (party) that executes public works. tax_id/reg_id de-duplicate the name. "
   "PII (address/pan/dob/photo) is intentionally excluded and never published.")),

 # ---- works side: NEW lifecycle nouns ----
 "works.sanction": ("0.1.0", noun("works.sanction","entity",["sanction_ref","sanction_kind"],
   {"work_code":NS(),"sanction_ref":S(),"sanction_date":NS(),
    "sanction_kind":{"type":"string","enum":["administrative","technical"]},"is_modified":NB(),
    "sanctioned_amount":NN(),"authority":NS(),"order_ref":NS(),"budget_ref":NS(),
    "scheme_name":NS(),"rate_schedule_year":NS(),"package_ref":NS(),"prov":NO(),"confidence":NN()},
   "An administrative or technical sanction for a public work (one noun, role-tagged by "
   "sanction_kind). Links money sanctioned to the work_code.")),

 "works.agreement": ("0.1.0", noun("works.agreement","entity",["agreement_ref"],
   {"agreement_ref":S(),"agreement_date":NS(),"stamp_cert_ref":NS(),
    "contract_amount":NN(),"premium_pct":NN(),"tax_pct":NN(),
    "contractor_ref":NS(),"contractor_legal_name":NS(),"work_codes":NAR("string"),
    "sanction_refs":NAR("string"),"budget_ref":NS(),"tender_ref":NS(),
    "completion_period_days":NI(),"guarantee_months":NN(),"prov":NO(),"confidence":NN()},
   "A contract instance (agreement + stamp certificate). May cover several work_codes across "
   "several jurisdictions -> work_codes[] is many.")),

 "works.boq_item": ("0.1.0", noun("works.boq_item","record",["work_code","line_no"],
   {"work_code":S(),"line_no":S("integer"),"description":NS(),"unit":NS(),
    "quantity":NN(),"rate":NN(),"amount":NN(),"spec_refs":NAR("string"),"is_modified":NB(),"prov":NO()},
   "A priced line item of a work's Bill of Quantities / Schedule-B. spec_refs carry standard "
   "clause codes as data.")),

 "works.measurement_bill": ("0.1.0", noun("works.measurement_bill","record",["work_code","bill_no"],
   {"work_code":S(),"bill_no":S(),"bill_part":NS(),"work_order_ref":NS(),"budget_ref":NS(),
    "mb_ref":NS(),"mb_pages":NS(),"value_of_work_done":NN(),"reserve_amount":NN(),
    "previous_paid":NN(),"deductions":NO(),"net_payable":NN(),"cheque_ref":NS(),
    "paid_at":NS(),"is_final":NB(),"prov":NO()},
   "A running or final bill against measured work, with its deduction memorandum. Per-bill line; "
   "complements the works.payment summary.", subject="works-bill")),

 "works.completion": ("0.1.0", noun("works.completion","entity",["work_code"],
   {"work_code":S(),"estimate_amount":NN(),"accepted_tender_amount":NN(),"claimed_amount":NN(),
    "excess_amount":NN(),"savings_amount":NN(),"sanction_refs":NAR("string"),"funding_source":NS(),
    "scheduled_start":NS(),"scheduled_completion":NS(),"actual_completion":NS(),
    "delay_days":NI(),"eot_approved":NB(),"mb_ref":NS(),"contractor_ref":NS(),
    "segment_ref":NS(),"certified_satisfactory":NB(),"prov":NO(),"confidence":NN()},
   "The completion record of a public work: estimate vs tender vs claimed, planned vs actual "
   "dates, delay and excess/savings. The richest single accountability record.")),

 "works.quality_test": ("0.1.0", noun("works.quality_test","record",["work_code","test_name"],
   {"work_code":S(),"test_name":S(),"sample_no":NS(),"boq_quantity":NS(),
    "required_tests":NI(),"actual_tests":NI(),"sampled_at":NS(),"location":NS(),
    "chainage":NS(),"result_value":NS(),"conforms":NB(),"lab_name":NS(),"prov":NO()},
   "A quality-control test outcome for a work: required vs actual test counts and conformity.")),

 # ---- rollups (currently emitted but ungoverned): govern as derived records ----
 "works.ward_rollup": ("0.1.0", noun("works.ward_rollup","record",["ward"],
   {"ward":S(),"jurisdiction_ref":NS(),"works_count":NI(),"payments_with_amount":NI(),
    "paid_net_total":NN(),"paid_gross_total":NN(),"derived_from":NAR("string"),"method":NS(),"prov":NO()},
   "Per-ward expenditure aggregate derived from works.payment. Grouping key for a ward view.")),

 "works.city_summary": ("0.1.0", noun("works.city_summary","record",[],
   {"works_count":NI(),"wards_count":NI(),"contractors_count":NI(),"payments_with_amount":NI(),
    "paid_net_total":NN(),"paid_gross_total":NN(),"generated_at":NS(),
    "derived_from":NAR("string"),"method":NS(),"prov":NO()},
   "A single whole-agency expenditure summary derived from the works ledger.")),

 "works.contractor_rollup": ("0.1.0", noun("works.contractor_rollup","record",["contractor"],
   {"contractor":S(),"name":NS(),"works_count":NI(),"paid_net_total":NN(),"paid_gross_total":NN(),
    "derived_from":NAR("string"),"method":NS(),"prov":NO()},
   "Per-contractor expenditure aggregate derived from the works ledger.")),
}

def govern():
    # domains already registered (works, procurement) by govern_ped_schemas.py; ensure anyway.
    for dom,label in [("works","Public Works"),("procurement","Public Procurement")]:
        _,e=post(NET+"/v1/domains",{"domain":dom,"label":label},token=STOK)
        print(f"  domain {dom}:", "ok" if not e else e)
    ok=0
    for sid,(ver,defn) in SCHEMAS.items():
        # Steward direct publish (ADR-0110): the Network steward IS the schema authority.
        r,e=post(NET+"/v1/schemas",
                 {"schema_id":sid,"version":ver,"definition":defn,"sensitivity":"public"},token=STOK)
        print(f"  {sid}@{ver}: {'ok' if not e else e}")
        if not e: ok+=1
    print(f"GOVERNED {ok}/{len(SCHEMAS)}")

if __name__=="__main__":
    govern()
