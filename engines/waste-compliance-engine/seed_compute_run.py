"""Bootstrap the ADR-0091 Compute plane + its first recipe (waste.compliance-rank).

Governs the `compute` domain + the single generic `compute.run@0.1.0` verb (steward), then seeds a governed
RECIPE — a `platform.verb_alias` binding the recipe id `waste.compliance-rank` → the certified compute handler
`waste-compliance-rank`. After this, `run{recipe:"waste.compliance-rank", args:{floor,top_k}}` serves the same
compute the bespoke `waste-compliance-rank` verb does — one governed verb, the domain in the recipe. Non-breaking:
the bespoke verb still answers directly. Idempotent.
    docker exec airawat-app-host python3 /app/components/engines/waste-compliance-engine/seed_compute_run.py
"""
import json, os, urllib.request as u, urllib.error

IDN = os.environ.get("IDENTITY_URL", "http://identity:8093")
NET = os.environ.get("NETWORK_URL", "http://network:8090")
KRN = os.environ.get("KERNEL_API", "http://kernel:8080")


def post(url, body, hdr=None):
    r = u.Request(url, data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", **(hdr or {})}, method="POST")
    try:
        return json.loads(u.urlopen(r, timeout=30).read()), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code}:{e.read().decode()[:200]}"


def main():
    stok = (post(f"{IDN}/login", {"username": "steward", "tenant": "airawat"})[0] or {}).get("access_token")
    if not stok:
        print("STEWARD LOGIN FAILED"); return
    H = {"Authorization": "Bearer " + stok}

    # 1) govern the `compute` domain (idempotent)
    r, e = post(f"{NET}/v1/domains", {"domain": "compute", "label": "Compute plane — generic run(recipe) over certified compute components (ADR-0091)"}, H)
    print("domain compute:", "ok" if r else e)

    # 2) govern the single generic verb compute.run
    defn = {"modality": "request",
            "description": "Run a governed compute RECIPE (scan / rank / prioritise / rollup). The recipe binds a "
                           "certified compute component + handler; the domain is the recipe, not the verb. ADR-0091 Compute plane.",
            "request": {"type": "object", "required": ["recipe"],
                        "properties": {"recipe": {"type": "string", "description": "governed recipe id (a platform.verb_alias)"},
                                       "args": {"type": "object", "description": "recipe inputs (merged through the recipe's build)"}}},
            "response": {"type": "object", "description": "the compute output (recipe-specific)"}}
    r, e = post(f"{NET}/v1/interfaces", {"interface_id": "compute.run", "version": "0.1.0", "definition": defn}, H)
    print("interface compute.run@0.1.0:", "ok" if r else e)

    # 3) seed the first RECIPE — governed platform.verb_alias binding recipe id → compute handler
    recipe = {"schema": "platform.verb_alias", "id": "ds:airawat:platform/verb_alias/waste.compliance-rank",
              "tenant": "airawat", "alias": "waste.compliance-rank", "enabled": True,
              "target_verb": "waste-compliance-rank"}   # no build → args pass through to the handler (floor, top_k)
    r, e = post(f"{KRN}/v1/registry/objects", {"id": recipe["id"], "object": recipe}, H)
    print("recipe waste.compliance-rank → waste-compliance-rank:", "ok" if r else e)


if __name__ == "__main__":
    main()
