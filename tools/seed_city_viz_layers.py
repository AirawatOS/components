#!/usr/bin/env python3
"""Seed / reconcile the shared City map-layer catalogue (viz.layer, ds:airawat:viz/layer/*) from
tools/viz_layers/city-layers.json — the catalogue Visualize lists for any tenant that has the layer's data.

Until now these records were authored ad hoc on the legacy EC2 staging stack (colour polarity via color_scale, the
Environment / Built Environment / Mobility / People / Public Works grouping, and the layers added for the demo:
census density, walkability, transit gap, chokepoints, night lights 2014–2025, lake water extent 1995–2021). The JSON
is an export of that live catalogue (2026-11-13, prov stripped) so it is reproducible from git.

Default is a DRY RUN: prints, per layer, whether the live record is missing, identical, or which fields differ.
--apply writes the missing / different ones as the standards-body steward (idempotent; identical ones are skipped).
Run inside the app-host container (it reaches identity + kernel):
    docker exec -i <app-host> python3 - [--apply] < tools/seed_city_viz_layers.py   (with the JSON at $LAYERS or
    piped next to it — see LAYERS below)
Env: IDN (identity URL) · KERNEL (kernel URL) · LAYERS (path to city-layers.json) · STEWARD (user, default steward)
"""
import json, os, sys, urllib.error, urllib.request

IDN = os.environ.get("IDN", "http://identity:8093")
KERNEL = os.environ.get("KERNEL", "http://kernel:8080")
LAYERS = os.environ.get("LAYERS", os.path.join(os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else ".",
                                               "viz_layers", "city-layers.json"))
STEWARD = os.environ.get("STEWARD", "steward")
IGNORE = {"prov", "provenance"}


def post(url, body, tok=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read() or b"{}")


def diff(want, have):
    keys = (set(want) | set(have)) - IGNORE
    return sorted(k for k in keys if want.get(k) != have.get(k))


def main(apply):
    layers = json.load(open(LAYERS))
    tok = post(IDN + "/login", {"username": STEWARD})["access_token"]
    live = {x["object"]["id"]: x["object"] for x in
            post(KERNEL + "/v1/registry/resolve", {"filter": {"schema": "viz.layer"}, "limit": 1000}, tok).get("results", [])}
    todo = 0
    for L in layers:
        have = live.get(L["id"])
        d = None if have is None else diff(L, have)
        state = "missing" if have is None else ("same" if not d else "differs: " + ", ".join(d))
        if have is not None and not d:
            print("  =", L["layer_id"]); continue
        todo += 1
        if apply:
            try:
                post(KERNEL + "/v1/registry/objects", {"id": L["id"], "object": L}, tok); print("  ✓", L["layer_id"], "(" + state + ")")
            except urllib.error.HTTPError as e:
                print("  ✗", L["layer_id"], e.code, (e.read() or b"")[:160].decode(errors="replace"))
        else:
            print("  ~", L["layer_id"], "—", state)
    print(("applied" if apply else "dry run:"), todo, "of", len(layers), "need writing" if not apply else "written")


if __name__ == "__main__":
    main("--apply" in sys.argv)
