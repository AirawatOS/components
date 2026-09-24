#!/usr/bin/env python3
"""Amritsar Property-Tax-Gap S2 ops (run INSIDE airawat-app-host, as amritsar-admin). MODE via argv[1]:
  seedpolicy  -> write tax.match_policy: exact key-join ON, cadastre-as-footprint, unregistered = strong single signal
  verify      -> counts + a few samples for tax.assessment / geo.parcel / tax.candidate (read-only)
Env: IDN KERNEL TENANT (defaults for the docker network)."""
import os, sys, json, time, urllib.request, urllib.error

IDN = os.environ.get("IDN", "http://airawat-identity:8093"); KERNEL = os.environ.get("KERNEL", "http://kernel:8080")
TENANT = os.environ.get("TENANT", "amritsar"); MODE = sys.argv[1] if len(sys.argv) > 1 else "verify"

def login(realm, user):
    r = urllib.request.Request(f"{IDN}/realms/{realm}/login", data=json.dumps({"username": user}).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=15).read())["access_token"]

def post(url, body, tok):
    h = {"Content-Type": "application/json"}
    if tok: h["Authorization"] = "Bearer " + tok
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(),
                          headers=h, method="POST"), timeout=120).read() or b"{}"), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code} {e.read().decode()[:200]}"

tok = login(TENANT, TENANT + "-admin")

def resolve(schema):
    for body in ({"filter": {"schema": schema}}, {"schema": schema}):
        r, e = post(KERNEL + "/v1/registry/resolve", body, tok)
        if r is not None:
            rows = r.get("results") or r.get("objects") or r.get("records") or (r if isinstance(r, list) else [])
            out = []
            for x in rows:
                o = x.get("object") or x
                oid = o.get("id") or x.get("id")
                if oid and oid.startswith(f"ds:{TENANT}:"):
                    out.append((oid, o))
            return out
    return []

if MODE == "seedpolicy":
    pid = f"ds:{TENANT}:tax/match_policy/default"
    # NOTE: key_join is ON by default in the engine's DEFAULT_POLICY; the schema doesn't carry a top-level `link`
    # key, so we only persist the candidate tuning (cadastre-as-footprint + the unregistered signal weight).
    rec = {"schema": "tax.match_policy", "id": pid, "tenant": TENANT, "name": "Amritsar property-tax matching policy",
           "status": "active",
           "candidate": {"parcel_is_footprint": True, "min_confidence": 0.5,
                         "signal_weights": {"utility_no_tax": 0.4, "footprint_no_assessment": 0.7,
                                            "change_detected": 0.2, "under_assessed": 0.1}},
           "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    r, e = post(KERNEL + "/v1/registry/objects", {"id": pid, "object": rec}, tok)
    print("seedpolicy ERR" if e else "SEEDED policy", e or pid)

elif MODE == "verify":
    for sch in ("tax.assessment", "geo.parcel", "tax.candidate"):
        rows = resolve(sch)
        # split old (real) vs new masked (AMR-*) so we can see PII exposure
        amr = [1 for oid, _ in rows if "/AMR-" in oid]
        print(f"\n{sch}: {len(rows)} total  ({sum(amr)} masked AMR-*, {len(rows)-sum(amr)} other)")
        for oid, o in rows[:4]:
            keys = ("owner", "uid", "parcel_id", "ward", "confidence", "status", "source")
            print("  ", oid, {k: o.get(k) for k in keys if o.get(k) is not None})
