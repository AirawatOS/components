#!/usr/bin/env python3
"""Behavioural check — Ward Works joins per-ward works and tender figures onto the ward's cells.

Drives the real build() and read_all() with in-memory records shaped as staging holds them, and checks:
  • every cell of a ward gets that ward's works count + spend and tender count;
  • ward numbers are normalised ("150.0" / "Ward 150" / "Ward No-164" → "150" / "164");
  • a tender whose title names no ward is reported unplaced, never guessed; a ward with no works / tenders gets no record;
  • another tenant's records are ignored; the governed definitions accept the records;
  • read_all() pages past the 20,000-row query cap (38,737 tenders on staging) and stops on a repeated page.
Exit 1 on failure.
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import run as ww                       # noqa: E402
import govern_ward_works as gov        # noqa: E402

fails = []
def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + str(detail)[:200] if detail else ""))
    if not ok: fails.append(name)

T = "janaagraha"
DATA = {
    "space.ward_cell": [{"id": "ds:janaagraha:space/ward_cell/c1", "h3_id": "c1", "ward_no": "154", "ward_name": "Basavanagudi"},
                        {"id": "ds:janaagraha:space/ward_cell/c2", "h3_id": "c2", "ward_no": "154.0", "ward_name": "Basavanagudi"},
                        {"id": "ds:janaagraha:space/ward_cell/c3", "h3_id": "c3", "ward_no": "164", "ward_name": "Vidyapeeta ward"},
                        {"id": "ds:janaagraha:space/ward_cell/c4", "h3_id": "c4", "ward_no": "2", "ward_name": "Chowdeswari Ward"},
                        {"id": "ds:other:space/ward_cell/c9", "h3_id": "c9", "ward_no": "154"}],
    "city.cell_profile": [{"id": "ds:janaagraha:city/cell_profile/c1", "cell_ref": {"h3_id": "c1", "centroid": [77.57, 12.94]}}],
    "works.ward_rollup": [{"id": "ds:janaagraha:works/ward_rollup/154", "ward": "Ward 154", "works_count": 900, "paid_net_total": 4.2e8},
                          {"id": "ds:janaagraha:works/ward_rollup/164", "ward": 164, "works_count": 300, "paid_net_total": 1.1e8},
                          {"id": "ds:other:works/ward_rollup/2", "ward": "2", "works_count": 5}],
    "procurement.tender": [{"id": "ds:janaagraha:procurement/tender/a", "title": "Maintainance of Road … in Ward No-164 Vidyapeeta call 3", "published_at": "2023-10-01"},
                           {"id": "ds:janaagraha:procurement/tender/b", "title": "Maintenance of road, drain and footpath work in ward no 154 Basavanagudi", "published_at": "2019-04-02"},
                           {"id": "ds:janaagraha:procurement/tender/c", "title": "Desilting in WARD 154", "published_at": "2016-01-10"},
                           {"id": "ds:janaagraha:procurement/tender/d", "title": "Supply of streetlights (city-wide)"},
                           {"id": "ds:janaagraha:procurement/tender/e", "title": "Repairs", "ward": "2.0"},
                           # 2022+ tenders in BBMP's NEW 243-ward numbering — must NOT land on the 2010 ward of that number
                           {"id": "ds:janaagraha:procurement/tender/f", "title": "Road works in Ward No 164 Avalahalli", "published_at": "2023-05-01"},
                           {"id": "ds:janaagraha:procurement/tender/g", "title": "Footpath in ward 154", "published_at": "2024-02-01"},
                           {"id": "ds:janaagraha:procurement/tender/h", "title": "Drains in Ward No-230 Hemmigepura", "published_at": "2022-08-01"}],
}
works, tenders, st = ww.build(lambda s: list(DATA.get(s, [])), T, "2026-11-12T03:00:00Z")
W = {r["cell_ref"]["h3_id"]: r for r in works}; P = {r["cell_ref"]["h3_id"]: r for r in tenders}
check("every cell of ward 154 gets its works figures ('154.0' and 'Ward 154' normalised)",
      W.get("c1", {}).get("works_count") == 900 and W.get("c2", {}).get("paid_net_total") == 4.2e8 and W["c1"]["ward_no"] == "154", W)
check("ward 164 from an int ward field", W.get("c3", {}).get("works_count") == 300)
check("a ward with no works gets no works record (no zero-fill)", "c4" not in W)
check("another tenant's cell / rollup is ignored", "c9" not in W and "c9" not in P)
check("tenders counted per ward from titles ('Ward No-164', 'ward no 154', 'WARD 154') and a ward field ('2.0')",
      (P.get("c1", {}).get("tenders_count"), P.get("c3", {}).get("tenders_count"), P.get("c4", {}).get("tenders_count")) == (2, 1, 1), P)
check("a tender naming no ward is reported unplaced, not guessed", st["tenders_placed"] == 4 and st["unplaced_by_reason"].get("no ward in title") == 1, st)
check("0.1.1 a 2022+ tender in the NEW numbering never lands on the 2010 ward of that number (164 Avalahalli, bare 154, 230)",
      st["tenders_unplaced"] == 4 and st["unplaced_by_reason"].get("not a ward on the map (newer numbering)") == 1
      and st["unplaced_by_reason"].get("2022+ tender, ward name does not confirm the 2010 numbering") == 2, st["unplaced_by_reason"])
check("…while a 2022+ tender whose title names the 2010 ward (164 Vidyapeeta) is placed", P.get("c3", {}).get("tenders_count") == 1)
check("the centroid is carried for place names on hover", W["c1"]["cell_ref"].get("centroid") == [77.57, 12.94])
check("ids are the tenant's own and deterministic", W["c1"]["id"] == "ds:janaagraha:works/ward_cell/c1" and P["c3"]["id"] == "ds:janaagraha:procurement/ward_cell/c3")
try:
    import jsonschema
    jsonschema.validate(W["c1"], gov.defn(*gov.SCHEMAS[0]))
    jsonschema.validate(P["c3"], gov.defn(*gov.SCHEMAS[1])); ok, why = True, ""
except ImportError:
    d = gov.defn(*gov.SCHEMAS[0]); extra = set(W["c1"]) - set(d["properties"])
    ok, why = not extra and all(k in W["c1"] for k in d["required"]), extra
except Exception as e:
    ok, why = False, str(e)[:160]
check("the governed definitions accept the records", ok, why)

# 0.2.0 — yearly series by work-order year (staging dates: '2015-04-01', '01/04/2015', and a sanctioned_year '2023-24')
check("order_year reads the staging date formats",
      [ww.order_year(x) for x in ({"work_order_date": "2023-06-12"}, {"work_order_date": "01/04/2015"},
                                  {"work_order_date": "01-Apr-15"}, {"sanctioned_year": "2023-24"}, {})] == [2023, 2015, 2015, 2023, None])
Y = dict(DATA, **{
    "works.work_order": [{"id": "ds:janaagraha:works/work_order/1", "ward": "154", "work_order_date": "01/04/2015"},
                         {"id": "ds:janaagraha:works/work_order/2", "ward": "154", "work_order_date": "2015-09-01"},
                         {"id": "ds:janaagraha:works/work_order/3", "ward": "154", "work_order_date": "2019-01-20"},
                         {"id": "ds:janaagraha:works/work_order/4", "ward": "154"},                                   # undated
                         {"id": "ds:janaagraha:works/work_order/5", "ward": "164", "work_order_date": "2008-03-03"}],  # outside window
    "works.payment": [{"id": "p1", "work_order": "ds:janaagraha:works/work_order/1", "paid_net": 100.0},
                      {"id": "p2", "work_order": "ds:janaagraha:works/work_order/2", "paid_net": 50.0},
                      {"id": "p2b", "work_order": "ds:janaagraha:works/work_order/2", "paid_net": 25.0},
                      {"id": "p3", "work_order": "ds:janaagraha:works/work_order/3", "paid_net": 10.0}]})
ids_ok = all(p["id"].startswith("p") for p in Y["works.payment"])
Y["works.payment"] = [dict(p, id="ds:janaagraha:works/payment/" + p["id"]) for p in Y["works.payment"]]
works2, _, st2 = ww.build(lambda s: list(Y.get(s, [])), T, "2026-11-12T03:00:00Z")
W2 = {r["cell_ref"]["h3_id"]: r for r in works2}
ser = {e["t"]: e for e in W2.get("c1", {}).get("series", [])}
check("0.2.0 every year of the window is a frame (2012–2022)", sorted(ser) == [str(y) for y in range(2012, 2023)], sorted(ser))
check("…2015 = 2 works, ₹175 net (payments joined to their order); 2019 = 1 work, ₹10",
      (ser.get("2015", {}).get("works_count"), ser.get("2015", {}).get("paid_net_total"),
       ser.get("2019", {}).get("works_count"), ser.get("2019", {}).get("paid_net_total")) == (2, 175.0, 1, 10.0), (ser.get("2015"), ser.get("2019")))
check("…a year with no work orders is 0 for that ward (2016)", ser.get("2016") == {"t": "2016", "works_count": 0, "paid_net_total": 0.0})
check("…undated orders are counted and left out of the series", st2["work_orders_undated"] == 1)
check("…a ward whose only order is outside the window gets no series", "series" not in W2.get("c3", {}), W2.get("c3", {}).get("series"))
try:
    import jsonschema
    jsonschema.validate(W2["c1"], gov.defn(*gov.SCHEMAS[0])); ok, why = True, ""
except ImportError:
    d = gov.defn(*gov.SCHEMAS[0]); ok, why = "series" in d["properties"] and not (set(W2["c1"]) - set(d["properties"])), ""
except Exception as e:
    ok, why = False, str(e)[:160]
check("…the 0.2.0 works.ward_cell definition accepts the series", ok, why)

# paging: 3 pages of the cap, then a short page; a source that ignores the cursor (repeats page 1) must not loop forever
pages = {None: [{"id": "t%d" % i} for i in range(ww.PAGE)], "p2": [{"id": "u%d" % i} for i in range(ww.PAGE)], "p3": [{"id": "v1"}]}
nxt = {None: "p2", "p2": "p3", "p3": None}
got = ww.read_all("procurement.tender", call=lambda p, b: {"results": pages[b.get("cursor")], "next_cursor": nxt[b.get("cursor")]})
check("read_all pages past the 20,000-row cap", len(got) == 2 * ww.PAGE + 1, len(got))
rep = ww.read_all("x", call=lambda p, b: {"results": pages[None], "next_cursor": "again"})
check("…and stops when the source repeats a page", len(rep) == ww.PAGE, len(rep))
print("ALL PASS" if not fails else "FAILED: %d" % len(fails)); sys.exit(1 if fails else 0)
