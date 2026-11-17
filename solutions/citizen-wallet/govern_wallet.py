#!/usr/bin/env python3
"""Citizen wallet — the DigiLocker SANDBOX adapter's vocabulary + its tenant. Run in the staging app-host container
(steward). The sandbox stands in for DigiLocker until the deployment is onboarded as a DigiLocker Requester/Issuer
partner: same shapes (documents identified by a URI <issuer>-<doctype>-<docno>), simulated data.
  identity.issued_document      a document an issuer has issued to a person, held in the (sandbox) locker — sensitive
  credential.digilocker_document the signed credential a citizen gets in the Airawat wallet when they FETCH one
Tenant: digilocker-sandbox ("DigiLocker (sandbox)") — issues the fetched-document credentials."""
import json, urllib.error, urllib.request

NET, IDN = "http://network:8090", "http://identity:8093"
ID = {"type": "string", "pattern": r"^ds:[a-z0-9-]+:\S+$"}
S = {"type": ["string", "null"]}
T = {"type": "string"}
DETAILS = {"type": ["object", "null"], "description": "document-specific fields (e.g. property id, seller, buyer)",
           "additionalProperties": {"type": ["string", "number", "boolean", "null"]}}
SCHEMAS = [
 ("identity.issued_document", {"type": "object", "title": "identity.issued_document", "x-kind": "record", "x-subject": "issued document",
   "description": "A document an issuer (registration department, registrar, transport department, an Airawat department) has "
                  "issued to a person and that sits in their document locker (DigiLocker). Personal data — sensitive.",
   "additionalProperties": False, "required": ["schema", "id", "uri", "doc_type", "issuer_name", "holder_name"],
   "properties": {"schema": {"const": "identity.issued_document"}, "id": ID, "uri": T, "doc_type": T, "title": S,
                  "issuer_name": T, "issuer_id": S, "doc_no": S, "issued_on": S, "holder_name": T, "holder_ref": S,
                  "details": DETAILS, "pushed_from": S, "tenant": S, "created_at": S}}, "sensitive"),
 ("credential.digilocker_document", {"type": "object", "title": "credential.digilocker_document", "x-kind": "fragment",
   "description": "A document fetched from the citizen's DigiLocker into the Airawat wallet, signed by the locker (sandbox).",
   "additionalProperties": False, "required": ["uri", "doc_type", "issuer_name"],
   "properties": {"uri": T, "doc_type": T, "title": T, "issuer_name": T, "doc_no": T, "issued_on": T, "holder_name": T,
                  "source": T, "details": DETAILS}}, "internal"),
]


def post(u, b, t=None):
    h = {"Content-Type": "application/json"}
    if t: h["Authorization"] = "Bearer " + t
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(u, data=json.dumps(b).encode(), headers=h, method="POST"), timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, "%s %s" % (e.code, e.read().decode()[:240])


if __name__ == "__main__":
    tok = post(IDN + "/login", {"username": "steward"})[0]["access_token"]
    r, e = post(NET + "/v1/tenants/resolve", {"tenant_id": "digilocker-sandbox"})
    if not r:
        r, e = post(NET + "/v1/tenants", {"tenant_id": "digilocker-sandbox", "name": "DigiLocker (sandbox)", "environment": "production",
                                          "jurisdiction": "in"}, tok)
        print("tenant digilocker-sandbox", "registered" if r else "FAILED " + e)
    else:
        print("tenant digilocker-sandbox exists")
    for sid, d, sens in SCHEMAS:
        pr, perr = post(NET + "/v1/schema-requests", {"schema_id": sid, "version": "0.1.0", "definition": d, "x_kind": d["x-kind"],
                        "sensitivity": sens, "justification": "Citizen wallet — DigiLocker sandbox adapter."}, tok)
        if perr and "already published" in perr:
            print(sid, "already governed"); continue
        dr, derr = post(NET + "/v1/schema-requests/decide", {"schema_id": sid, "version": "0.1.0", "decision": "approve"}, tok)
        print(sid, sens, "approved" if dr else "FAILED " + str(perr or derr))
