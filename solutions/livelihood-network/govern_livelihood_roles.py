#!/usr/bin/env python3
"""Livelihood network — its governed DOMAIN and the network ROLE of a platform worker. Run in the staging app-host
container (standards body / steward), like the other govern_* scripts.
  domain  livelihood                    the livelihood network's namespace for roles and schemas
  role    livelihood.platform-worker    a person who works through a platform (driver, delivery partner, …); used as
                                        the AUDIENCE of platform-worker services (e.g. a platform's partner
                                        registration), so they are listed for platform workers, not for every citizen.
A tenant's own member roles (Ola's "partner") stay the tenant's (ADR-0136); this is the network role they map to."""
import json, urllib.error, urllib.request

NET, IDN = "http://network:8090", "http://identity:8093"


def post(u, b, t=None):
    h = {"Content-Type": "application/json"}
    if t: h["Authorization"] = "Bearer " + t
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(u, data=json.dumps(b).encode(), headers=h, method="POST"), timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, "%s %s" % (e.code, e.read().decode()[:240])


if __name__ == "__main__":
    tok = post(IDN + "/login", {"username": "steward"})[0]["access_token"]
    have = {d["domain"] for d in json.loads(urllib.request.urlopen(NET + "/v1/domains", timeout=10).read())["domains"]}
    if "livelihood" in have:
        print("domain livelihood already governed")
    else:
        r, e = post(NET + "/v1/domains", {"domain": "livelihood", "label": "Livelihood (workers, platforms, skills, benefits)"}, tok)
        print("domain livelihood", "governed" if r else "FAILED " + e)
    roles = {x["role"] for x in json.loads(urllib.request.urlopen(NET + "/v1/roles", timeout=10).read())["roles"]}
    if "livelihood.platform-worker" in roles:
        print("role livelihood.platform-worker already governed")
    else:
        r, e = post(NET + "/v1/roles", {"role": "livelihood.platform-worker", "label": "Platform worker",
                                        "definition": "A person who works through a platform (driver, delivery partner, and similar). "
                                                      "The audience of platform-worker services on the livelihood network."}, tok)
        print("role livelihood.platform-worker", "governed" if r else "FAILED " + e)
