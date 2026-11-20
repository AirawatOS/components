#!/usr/bin/env python3
"""Livelihood network — the PLATFORM WORK CERTIFICATE credential type. Run in the staging app-host container (steward).
Any platform on the network (Ola, Swiggy, ...) issues it to a worker's wallet once the platform has verified the worker:
identity from the platform's own Aadhaar-based KYC (with the KYC trail), and the engagement (occupation, the worker's
ID on the platform, since when, status). eShram's connector reads it to register the worker or link their UAN without
data entry; the worker can download it as a PDF with a signed QR code.
  credential.platform_work_certificate   issued by a platform tenant, held by the worker (governed by MoLE on a real network)
Never carries the Aadhaar number, VID, biometrics or the raw eKYC response — only the last 4 digits."""
import json, urllib.error, urllib.request

NET, IDN = "http://network:8090", "http://identity:8093"
S = {"type": ["string", "null"]}
T = {"type": "string"}
SID = "credential.platform_work_certificate"
DEF = {"type": "object", "title": SID, "x-kind": "fragment",
       "description": "A platform's certificate that a worker works with it, issued to the worker's wallet: the identity the "
                      "platform verified through Aadhaar eKYC (with how and when), the engagement (occupation, the worker's ID "
                      "on the platform, start date) and its status. Used to register on eShram without data entry.",
       "additionalProperties": False, "required": ["certificate_no", "platform", "full_name", "status", "issued_on"],
       "properties": {"certificate_no": T, "platform": T, "worker_platform_id": S, "occupation": S, "engaged_since": S,
                      "status": {"type": "string", "enum": ["active", "ended"]}, "full_name": T, "date_of_birth": S,
                      "gender": S, "address": S, "mobile": S, "aadhaar_last4": {"type": ["string", "null"], "pattern": "^[0-9]{4}$"},
                      "kyc_method": S, "kyc_verified_on": S, "kyc_by": S, "issued_on": T}}


def post(u, b, t=None):
    h = {"Content-Type": "application/json"}
    if t: h["Authorization"] = "Bearer " + t
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(u, data=json.dumps(b).encode(), headers=h, method="POST"), timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, "%s %s" % (e.code, e.read().decode()[:240])


if __name__ == "__main__":
    tok = post(IDN + "/login", {"username": "steward"})[0]["access_token"]
    pr, perr = post(NET + "/v1/schema-requests", {"schema_id": SID, "version": "0.1.0", "definition": DEF, "x_kind": "fragment",
                    "sensitivity": "internal", "justification": "Livelihood network - platform work certificate (ADR-0135)."}, tok)
    if perr and "already published" in perr:
        print(SID, "already governed")
    else:
        dr, derr = post(NET + "/v1/schema-requests/decide", {"schema_id": SID, "version": "0.1.0", "decision": "approve"}, tok)
        print(SID, "approved" if dr else "FAILED " + str(perr or derr))
