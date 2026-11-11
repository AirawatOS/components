#!/usr/bin/env python3
"""works-ward-rollup — recompute the materialised aggregates the Public Expenditure Dashboard reads for its
overview: works.ward_rollup (one row per ward: works_count + ₹ paid) + works.city_summary (one city total).

Why a rollup: the dashboard must show total ₹ paid and spend-by-ward over ~40k work orders. /bridge/query is a
row reader (it has count/distinct but no SUM), so summing client-side would page forever. This driver does the
GROUP BY once — reading the governed works.work_order + works.payment via the broker (ctx.facts pages internally)
— and writes ~200 tiny rows the app can read instantly. Re-run after each ingest to refresh.

aos SDK / clean-room: no egress; the only I/O is governed reads + writes over the broker. ₹ totals are summed
ONLY from payments that already carry amounts (the extractor's consistency gate decided that upstream)."""
import hashlib
from aos import driver, ctx, now


def _obj(r):
    return r.get("object", r) if isinstance(r, dict) else r


@driver.main
def run():
    if not ctx.tenant:
        raise SystemExit("FATAL: TENANT not set.")
    T = ctx.tenant

    # 1) work_order → ward, and works-count per ward
    ward_of, works_count = {}, {}
    n_wo = 0
    for r in ctx.facts("works.work_order"):
        o = _obj(r)
        wid = o.get("id")
        if not wid:
            continue
        ward = (o.get("ward") or "(unspecified)")
        ward_of[wid] = ward
        works_count[ward] = works_count.get(ward, 0) + 1
        n_wo += 1

    # 2) sum ₹ by ward from payments that carry amounts (join via work_order ref)
    agg = {}   # ward -> [net, gross, payments_with_amount]
    for r in ctx.facts("works.payment"):
        p = _obj(r)
        ward = ward_of.get(p.get("work_order"), "(unspecified)")
        a = agg.setdefault(ward, [0.0, 0.0, 0])
        net, gross = p.get("paid_net"), p.get("paid_gross")
        if net is not None:
            a[0] += float(net); a[2] += 1
        if gross is not None:
            a[1] += float(gross)

    # 3) distinct contractors (city total)
    n_contractors = sum(1 for _ in ctx.facts("works.contractor"))

    ts = now()
    rollups = []
    city_net = city_gross = 0.0
    city_pwa = 0
    for ward, cnt in works_count.items():
        net, gross, pwa = agg.get(ward, [0.0, 0.0, 0])
        city_net += net; city_gross += gross; city_pwa += pwa
        rid = f"ds:{T}:works/ward_rollup/{hashlib.sha1(ward.encode()).hexdigest()[:12]}"
        rollups.append({"schema": "works.ward_rollup", "id": rid, "tenant": T, "ward": ward,
                        "works_count": cnt, "payments_with_amount": pwa,
                        "paid_net_total": round(net, 2), "paid_gross_total": round(gross, 2),
                        "prov": {"source": "opencity-bbmp", "generated_at": ts, "component": "works-ward-rollup@0.1.0"}})

    city = {"schema": "works.city_summary", "id": f"ds:{T}:works/city_summary/bbmp", "tenant": T,
            "works_count": n_wo, "wards_count": len(works_count), "contractors_count": n_contractors,
            "payments_with_amount": city_pwa, "paid_net_total": round(city_net, 2),
            "paid_gross_total": round(city_gross, 2), "generated_at": ts,
            "prov": {"source": "opencity-bbmp", "component": "works-ward-rollup@0.1.0"}}

    # CONTRACTOR rollup: ₹ + works per contractor (join work_order → contract → contractor)
    wo_contractor, cname = {}, {}
    for r in ctx.facts("works.contract"):
        o = _obj(r); wo_contractor[o.get("work_order")] = o.get("contractor")
    for r in ctx.facts("works.contractor"):
        o = _obj(r); cname[o.get("id")] = o.get("name")
    cworks, cagg = {}, {}
    for woid, cc in wo_contractor.items():
        if cc:
            cworks[cc] = cworks.get(cc, 0) + 1
    for r in ctx.facts("works.payment"):
        p = _obj(r); cc = wo_contractor.get(p.get("work_order"))
        if not cc:
            continue
        a = cagg.setdefault(cc, [0.0, 0.0])
        if p.get("paid_net") is not None:
            a[0] += float(p["paid_net"])
        if p.get("paid_gross") is not None:
            a[1] += float(p["paid_gross"])
    crolls = []
    for cc, cnt in cworks.items():
        net, gross = cagg.get(cc, [0.0, 0.0])
        rid = f"ds:{T}:works/contractor_rollup/{hashlib.sha1(cc.encode()).hexdigest()[:12]}"
        crolls.append({"schema": "works.contractor_rollup", "id": rid, "tenant": T, "contractor": cc,
                       "name": cname.get(cc), "works_count": cnt,
                       "paid_net_total": round(net, 2), "paid_gross_total": round(gross, 2),
                       "prov": {"source": "opencity-bbmp", "generated_at": ts, "component": "works-ward-rollup@0.2.0"}})

    allobjs = rollups + [city] + crolls
    for i in range(0, len(allobjs), 2000):          # broker batch cap is 5000; chunk well under it
        ctx.write_many(allobjs[i:i + 2000])
    print(f"[works-ward-rollup] wards={len(works_count)} works={n_wo} contractors={n_contractors} "
          f"payments_with_amount={city_pwa} net=₹{round(city_net/1e7,1)}cr gross=₹{round(city_gross/1e7,1)}cr "
          f"→ wrote {len(rollups)+1+len(crolls)} rollup records ({len(crolls)} contractor)", flush=True)
    return 0


if __name__ == "__main__":
    driver.run()
