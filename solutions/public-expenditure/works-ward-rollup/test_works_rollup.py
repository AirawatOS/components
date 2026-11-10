#!/usr/bin/env python3
"""Behavioural check — works-ward-rollup computes the ward, city AND contractor rollups (commit 61bd459).

Runs the real driver against the aos in-memory registry with FIXED works.* facts:
  • works.contractor_rollup: one row per contractor — works_count via work_order → contract → contractor, and the
    ₹ net/gross summed from that contractor's payments; a contractor with contracts but no payments shows 0;
  • a payment on a work order with no contract adds to the ward/city totals but to no contractor;
  • works.ward_rollup counts works and sums ₹ per ward; works.city_summary totals agree with the wards.
Exit 1 on failure.
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "_common"))
from aos import driver  # noqa: E402
import run as _wr       # noqa: E402,F401  (registers the driver)

fails = []


def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + detail if detail else ""))
    if not ok:
        fails.append(name)


T = "testcity"
seed = {}
for o in [
    {"schema": "works.work_order", "id": "wo1", "ward": "Ward 1"}, {"schema": "works.work_order", "id": "wo2", "ward": "Ward 1"},
    {"schema": "works.work_order", "id": "wo3", "ward": "Ward 2"}, {"schema": "works.work_order", "id": "wo4", "ward": "Ward 2"},
    {"schema": "works.contractor", "id": "k-acme", "name": "Acme Roads"}, {"schema": "works.contractor", "id": "k-bolt", "name": "Bolt Infra"},
    {"schema": "works.contract", "id": "ct1", "work_order": "wo1", "contractor": "k-acme"},
    {"schema": "works.contract", "id": "ct2", "work_order": "wo2", "contractor": "k-acme"},
    {"schema": "works.contract", "id": "ct3", "work_order": "wo3", "contractor": "k-bolt"},
    {"schema": "works.payment", "id": "p1", "work_order": "wo1", "paid_net": 100000.0, "paid_gross": 118000.0},
    {"schema": "works.payment", "id": "p2", "work_order": "wo2", "paid_net": 50000.0, "paid_gross": 59000.0},
    {"schema": "works.payment", "id": "p3", "work_order": "wo4", "paid_net": 7000.0, "paid_gross": 8260.0},   # wo4 has NO contract
]:
    seed[o["id"]] = o

store = driver.run_local(seed, tenant=T)
cr = {o["contractor"]: o for o in store.values() if o.get("schema") == "works.contractor_rollup"}
wr = {o["ward"]: o for o in store.values() if o.get("schema") == "works.ward_rollup"}
city = [o for o in store.values() if o.get("schema") == "works.city_summary"]
check("one contractor rollup per contractor with a contract", sorted(cr) == ["k-acme", "k-bolt"], str(sorted(cr)))
check("Acme: 2 works, ₹150000 net / ₹177000 gross, name joined",
      (cr.get("k-acme", {}).get("works_count"), cr.get("k-acme", {}).get("paid_net_total"), cr.get("k-acme", {}).get("paid_gross_total"),
       cr.get("k-acme", {}).get("name")) == (2, 150000.0, 177000.0, "Acme Roads"), str(cr.get("k-acme")))
check("Bolt: 1 work, no payments → ₹0", (cr.get("k-bolt", {}).get("works_count"), cr.get("k-bolt", {}).get("paid_net_total")) == (1, 0.0))
check("the uncontracted payment (wo4) reaches no contractor", sum(o.get("paid_net_total", 0) for o in cr.values()) == 150000.0)
check("ward rollups: Ward 1 = 2 works ₹150000, Ward 2 = 2 works ₹7000",
      (wr.get("Ward 1", {}).get("works_count"), wr.get("Ward 1", {}).get("paid_net_total"),
       wr.get("Ward 2", {}).get("works_count"), wr.get("Ward 2", {}).get("paid_net_total")) == (2, 150000.0, 2, 7000.0))
check("city summary agrees: 4 works, 2 wards, 2 contractors, ₹157000 net",
      len(city) == 1 and (city[0].get("works_count"), city[0].get("wards_count"), city[0].get("contractors_count"),
                          city[0].get("paid_net_total")) == (4, 2, 2, 157000.0), str(city[:1]))

print("ALL PASS" if not fails else "FAILED: %d" % len(fails))
sys.exit(1 if fails else 0)
