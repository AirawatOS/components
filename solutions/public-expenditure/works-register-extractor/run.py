#!/usr/bin/env python3
"""works-register-extractor — BBMP public works from the OpenCity open datasets (ODbL) → governed public-works
facts for the Public Expenditure Dashboard: works.work_order (+ works.contractor, works.contract, works.payment),
each carrying full provenance back to the source CSV.

aos SDK (clean-room / ADR-0030): the ONLY I/O is the broker. The CSV is fetched via ctx.fetch (brokered,
declared egress); governed records are written via ctx.write; the write-tenant is ctx.tenant. No raw sockets —
so it runs identically in the dev sandbox (Studio Test, L1) and when installed.

SOURCE-AGNOSTIC + SELF-VALIDATING AMOUNTS. BBMP publishes two shapes of "work order" CSV:
  • IFMS "Work Orders by Ward" (default here): columns wo num / wodetails / contractor / amount / deduction / nett
    — internally CONSISTENT (amount − deduction == nett on every row), so the money is TRUSTWORTHY.
  • the dept-wise "Work Orders & Bill Payment" register: Total / Deduction / Net Payment — DENORMALISED and
    inconsistent (Deduction/Net can exceed Total), so its money is NOT trustworthy.
This driver maps either via case-insensitive column fallbacks, and populates works.payment amounts ONLY from rows
that pass the consistency gate (gross − deduction == net). A work order whose source rows don't validate gets the
INVENTORY (work + contractor + dates) with amounts left empty + a prov note — never a fabricated figure.
The join key is the work-order / job number (dept-FY-serial, e.g. 56-15-000012), the same key across BBMP sources.

REAL data only: a fetch error fails loudly; ZERO work-order rows is a legitimate empty result. The full raw row
is preserved under prov.raw.

  Env: OPENCITY_CSV_URL (any BBMP work-order CSV; default = IFMS ward "A Narayanapura (56)") ·
       WARD (optional label for the file's ward) · MAX_ROWS (optional cap; default 0 = whole file).
"""
import csv, io, os, hashlib
from aos import driver, ctx, egress, now

SRC = "ds:janaagraha:ref/source/opencity-bbmp"
# IFMS "Work Orders by Ward (2013-2022)" — self-consistent amounts (amount−deduction==nett). Override via env.
DEFAULT_CSV = ("https://data.opencity.in/dataset/d22cea6d-5256-43af-8ad5-68db551e0760/"
               "resource/a1292773-8c42-47b6-ba06-1efe1d7002af/download/ff76f475-9389-4f51-9d4e-debaa9a68b80.csv")

egress("data.opencity.in", rate_per_min=30,
       user_agent="AirawatOS/works-register-extractor (+https://airawat.org; manishsv@gmail.com)",
       provides=["works.work_order", "works.contract", "works.contractor", "works.payment"], source_ref=SRC)


def _num(v):
    try:
        return float(str(v).replace(",", "").strip()) if v not in (None, "", "NA", "N/A", "-") else None
    except Exception:
        return None


def _pick(row, *names):
    """Case/space-insensitive column lookup with fallbacks — BBMP CSVs vary header wording across sources."""
    low = {str(k).lower().strip(): v for k, v in row.items() if k}
    for n in names:
        v = low.get(n.lower().strip())
        if v not in (None, ""):
            return v
    return None


@driver.main
def run():
    if not ctx.tenant:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant.")
    T = ctx.tenant
    url = os.environ.get("OPENCITY_CSV_URL", DEFAULT_CSV)
    ward = (os.environ.get("WARD") or "").strip() or None
    cap = int(os.environ.get("MAX_ROWS", "0") or 0)

    r = ctx.fetch(url)
    if not getattr(r, "ok", False) or not r.body:
        raise SystemExit(f"FATAL: OpenCity fetch failed (status={getattr(r, 'status', None)}) for {url}")
    text = r.body.decode("utf-8", "replace") if isinstance(r.body, (bytes, bytearray)) else str(r.body)
    # Some OpenCity resources prepend a title banner ("Bruhat Bengaluru… / Works Bill Public View") before the
    # real header, so the first CSV line isn't the columns. Find the header row: the first line that names a
    # work-order key AND a data column. Fall back to line 0 if nothing matches (already-clean files).
    lines = text.splitlines()
    hdr = 0
    for i, ln in enumerate(lines[:15]):
        ll = ln.lower()
        if (("wo num" in ll) or ("work order" in ll) or ("job number" in ll)) and \
           (("amount" in ll) or ("nett" in ll) or ("contractor" in ll) or ("details" in ll) or ("deduction" in ll)):
            hdr = i
            break
    rows = list(csv.DictReader(io.StringIO("\n".join(lines[hdr:]))))
    capped = bool(cap) and len(rows) > cap
    if capped:
        print(f"[works-register-extractor] NOTE: MAX_ROWS={cap} — ingesting first {cap} of {len(rows)} rows.")
        rows = rows[:cap]

    ts = now()
    wo, ct, cr, pay = {}, {}, {}, {}
    for row in rows:
        wo_no = str(_pick(row, "wo num", "work order number", "work order no", "work_order_no",
                          "job number", "job no", "wo no", "work order") or "").strip()
        if not wo_no:
            continue
        key = hashlib.sha1((wo_no + "|" + url).encode()).hexdigest()[:12]
        wo_id = f"ds:{T}:works/work_order/{key}"
        # a dept-FY-serial value (e.g. 56-15-000012 / 308-14-000028) IS the BBMP job number
        job = _pick(row, "job number", "job no", "job_number") or (wo_no if wo_no.count("-") == 2 else None)
        if wo_id not in wo:
            wo[wo_id] = {"schema": "works.work_order", "id": wo_id, "tenant": T, "work_order_no": wo_no,
                         "job_number": job,
                         "work_name": _pick(row, "wodetails", "work order details", "work name", "work_name", "name of work", "description"),
                         "ward": ward or _pick(row, "ward", "ward no", "ward name"),
                         "zone": _pick(row, "zone", "zone name"),
                         "work_order_date": _pick(row, "order date", "work order date", "wo date", "date"),
                         "prov": {"source": "opencity-bbmp", "source_ref": SRC, "source_url": url, "retrieved": ts, "raw": row}}

        cname = _pick(row, "contractor", "contractor name", "name of contractor", "agency", "agency name")
        contractor_id = None
        if cname:
            contractor_id = f"ds:{T}:works/contractor/{hashlib.sha1(str(cname).encode()).hexdigest()[:12]}"
            cr.setdefault(contractor_id, {"schema": "works.contractor", "id": contractor_id, "tenant": T, "name": str(cname).strip()})

        cid = f"ds:{T}:works/contract/{key}"
        if cid not in ct:
            ct[cid] = {"schema": "works.contract", "id": cid, "tenant": T, "work_order": wo_id,
                       "contractor": contractor_id, "prov": {"source": "opencity-bbmp"}}

        # payment: aggregate per work order, but ONLY from rows whose amounts self-validate (gross − deduction == net)
        pid = f"ds:{T}:works/payment/{key}"
        p = pay.setdefault(pid, {"_gross": 0.0, "_net": 0.0, "_lines": 0, "_ok": 0})
        gross = _num(_pick(row, "amount", "total amount in rs", "gross", "bill amount"))
        ded = _num(_pick(row, "deduction", "deduction in rs")) or 0.0
        net = _num(_pick(row, "nett", "net payment in rs", "net amount", "paid amount"))
        p["_lines"] += 1
        if gross is not None and net is not None and abs(gross - ded - net) < 1.0:   # self-consistent bill
            p["_gross"] += gross
            p["_net"] += net
            p["_ok"] += 1

    if not wo:
        head = (list(rows[0].keys()) if rows else [])
        raise SystemExit(f"DIAG: parsed {len(rows)} rows, matched 0 work-orders. header={head}")

    # finalise payments: trustworthy figures only when ≥1 bill passed the consistency gate; else amounts empty
    pay_objs = []
    for pid, agg in pay.items():
        key = pid.rsplit("/", 1)[-1]
        rec = {"schema": "works.payment", "id": pid, "tenant": T, "work_order": f"ds:{T}:works/work_order/{key}",
               "prov": {"source": "opencity-bbmp", "source_url": url, "bill_lines": agg["_lines"]}}
        if agg["_ok"] > 0:
            rec["paid_gross"] = round(agg["_gross"], 2)
            rec["paid_net"] = round(agg["_net"], 2)
            rec["prov"]["consistent_bills"] = agg["_ok"]
        else:
            rec["prov"]["amounts_note"] = "source rupee amounts failed the gross−deduction==net consistency check; not mapped"
        pay_objs.append(rec)

    objs = list(wo.values()) + list(cr.values()) + list(ct.values()) + pay_objs
    for o in objs:
        ctx.write(o)
    with_amt = sum(1 for p in pay_objs if "paid_net" in p)
    print(f"[works-register-extractor] OpenCity rows={len(rows)}{' (capped)' if capped else ''} → "
          f"work_orders={len(wo)} contracts={len(ct)} contractors={len(cr)} payments={len(pay_objs)} "
          f"({with_amt} with trustworthy amounts, rest inventory-only) total_records={len(objs)} from {url}", flush=True)
    return 0


if __name__ == "__main__":
    driver.run()
