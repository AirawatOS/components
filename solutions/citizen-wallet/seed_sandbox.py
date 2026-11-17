#!/usr/bin/env python3
"""DigiLocker SANDBOX — fictional issued documents for the urban services demo stories. Runs INSIDE the staging
app-host container as the sandbox's fulfilment principal (password from the server-side secrets file; never printed).
Stories (all people fictional; issuers labelled "(sandbox)"):
  Neha Kapoor     bought UP-CDA-000295 from Aadhya Khandelwal → sale deed      → applies for name transfer (sale)
  Kabir Bairwa    son of the late Zoya Bairwa (UP-CDA-000264)  → death + succession certificates → name transfer (inheritance)
  Ira Choudhary   holds UP-CDA-000258                          → her purchase deed → links the property to her account
  Ishaan Saxena   holds UP-CDA-000002                          → identity documents only"""
import json, urllib.error, urllib.request

KERNEL, IDN, T = "http://kernel:8080", "http://identity:8093", "digilocker-sandbox"
REG, RBD, COURT, ITD, TRN = ("Department of Registration and Stamps (sandbox)", "Registrar of Births and Deaths (sandbox)",
                             "Civil Court (sandbox)", "Income Tax Department (sandbox)", "Transport Department (sandbox)")
DOCS = [
 ("Neha Kapoor", "sale-deed", "Registered sale deed", REG, "in.gov.sandbox.igrs", "RS-2026-004127", "2026-10-28",
  {"property_id": "UP-CDA-000295", "seller": "Aadhya Khandelwal", "buyer": "Neha Kapoor", "consideration_inr": 4800000,
   "sub_registrar": "Sub-Registrar Office II (sandbox)"}),
 ("Neha Kapoor", "identity-proof", "PAN card", ITD, "in.gov.sandbox.pan", "AAAPK0001X", "2015-06-02", {}),
 ("Neha Kapoor", "identity-proof", "Driving licence", TRN, "in.gov.sandbox.transport", "DL-0420150001234", "2015-09-14",
  {"valid_till": "2035-09-13"}),
 ("Kabir Bairwa", "death-certificate", "Death certificate — Zoya Bairwa", RBD, "in.gov.sandbox.crs", "D-2026-0007781", "2026-08-03",
  {"deceased": "Zoya Bairwa", "date_of_death": "2026-07-29", "informant": "Kabir Bairwa"}),
 ("Kabir Bairwa", "succession-certificate", "Succession certificate", COURT, "in.gov.sandbox.ecourts", "SC-118-2026", "2026-10-09",
  {"deceased": "Zoya Bairwa", "heir": "Kabir Bairwa", "relation": "son", "property_id": "UP-CDA-000264"}),
 ("Kabir Bairwa", "identity-proof", "PAN card", ITD, "in.gov.sandbox.pan", "AAAPB0002Y", "2018-01-22", {}),
 ("Ira Choudhary", "sale-deed", "Registered sale deed (purchase)", REG, "in.gov.sandbox.igrs", "RS-2014-001893", "2014-03-17",
  {"property_id": "UP-CDA-000258", "buyer": "Ira Choudhary", "consideration_inr": 2150000}),
 ("Ira Choudhary", "identity-proof", "PAN card", ITD, "in.gov.sandbox.pan", "AAAPC0003Z", "2009-11-05", {}),
 ("Ishaan Saxena", "identity-proof", "PAN card", ITD, "in.gov.sandbox.pan", "AAAPS0004W", "2012-04-19", {}),
]


def post(u, b, t=None):
    h = {"Content-Type": "application/json"}
    if t: h["Authorization"] = "Bearer " + t
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(u, data=json.dumps(b).encode(), headers=h, method="POST"), timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, "%s %s" % (e.code, e.read().decode()[:200])


fp = json.load(open("/data/app-bundles/.secrets/fulfil-principals.json"))[T]
tok = post(IDN + "/login", {"username": fp["username"], "tenant": T, "password": fp["password"]})[0]["access_token"]
ok = 0
for holder, dtype, title, issuer, issuer_id, no, on, det in DOCS:
    uri = "%s-%s-%s" % (issuer_id, dtype.upper().replace("-", ""), no)
    rid = "ds:%s:identity/issued_document/%s" % (T, uri.replace(".", "-").lower())
    o = {"schema": "identity.issued_document", "id": rid, "tenant": T, "uri": uri, "doc_type": dtype, "title": title,
         "issuer_name": issuer, "issuer_id": issuer_id, "doc_no": no, "issued_on": on, "holder_name": holder, "details": det or None}
    r, e = post(KERNEL + "/v1/registry/objects", {"id": rid, "object": o}, tok)
    if r: ok += 1
    else: print("FAILED", title, e)
print("sandbox documents written: %d of %d" % (ok, len(DOCS)))
