#!/usr/bin/env python3
"""eShram SANDBOX participant — its tenant and credential vocabulary (ADR-0135 worked example). Run in the staging
app-host container (steward). The sandbox stands in for eShram until the Ministry's connector is granted access to the
real platform-worker registration APIs: same step shapes, simulated data, sandbox UANs ("SBX" + 9 digits).
  credential.eshram_registration  the worker's eShram registration (UAN, occupation, linked platforms), signed by eShram
Tenant: eshram-sandbox ("eShram (sandbox)") — also issues credential.identity_verified (Aadhaar OTP eKYC via eShram)."""
import json, urllib.error, urllib.request

NET, IDN = "http://network:8090", "http://identity:8093"
S = {"type": ["string", "null"]}
T = {"type": "string"}
SCHEMAS = [
 ("credential.eshram_registration", {"type": "object", "title": "credential.eshram_registration", "x-kind": "fragment",
   "description": "A worker's registration on eShram, the national database of unorganised and platform workers: the UAN, "
                  "main occupation and the platforms the worker is linked to. Issued to the worker's wallet by eShram.",
   "additionalProperties": False, "required": ["uan", "registered_on", "status"],
   "properties": {"uan": T, "full_name": S, "registered_on": T, "occupation": S,
                  "status": {"type": "string", "enum": ["active", "inactive"]}, "source": S,
                  "platforms": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                "properties": {"name": S, "platform_id": {"type": ["integer", "string"]},
                                               "worker_platform_id": S, "since": S}}}}}, "internal"),
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
    r, e = post(NET + "/v1/tenants/resolve", {"tenant_id": "eshram-sandbox"})
    if not r:
        r, e = post(NET + "/v1/tenants", {"tenant_id": "eshram-sandbox", "name": "eShram (sandbox)", "environment": "production",
                                          "jurisdiction": "in"}, tok)
        print("tenant eshram-sandbox", "registered" if r else "FAILED " + e)
    else:
        print("tenant eshram-sandbox exists")
    for sid, d, sens in SCHEMAS:
        pr, perr = post(NET + "/v1/schema-requests", {"schema_id": sid, "version": "0.1.0", "definition": d, "x_kind": d["x-kind"],
                        "sensitivity": sens, "justification": "Livelihood network - eShram sandbox participant (ADR-0135)."}, tok)
        if perr and "already published" in perr:
            print(sid, "already governed"); continue
        dr, derr = post(NET + "/v1/schema-requests/decide", {"schema_id": sid, "version": "0.1.0", "decision": "approve"}, tok)
        print(sid, sens, "approved" if dr else "FAILED " + str(perr or derr))
