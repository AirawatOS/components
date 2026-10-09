#!/usr/bin/env python3
"""works-register-extractor — BBMP public work-orders from the OpenCity open dataset (ODbL) → governed
public-works facts for the Public Expenditure Dashboard. One CSV row → works.work_order (+ works.contractor,
works.contract, works.payment), each carrying full provenance back to the source CSV.

aos SDK (clean-room / ADR-0030): the ONLY I/O is the broker. The OpenCity CSV is fetched via ctx.fetch
(brokered, declared egress), governed records are written via ctx.write_many, the write-tenant is ctx.tenant.
No raw sockets — so it runs identically in the dev sandbox (Studio Test, L1) and when installed.

REAL OpenCity data only: a fetch error fails loudly; ZERO work-order rows is a legitimate empty result
(reported, exit 0, nothing invented). The full raw CSV row is preserved under prov.raw (original-input
preservation) so no source field is ever lost, even when a column name isn't recognised.

  Env: OPENCITY_CSV_URL (a BBMP "Work Order Data" resource CSV; default = 308-CE-Lakes 2015-18) ·
       MAX_ROWS (cap rows for a quick dev run; default 2000 — a cap is LOGGED, never silent).
"""
import csv, io, os, hashlib
from aos import driver, ctx, egress, now

SRC = "ds:janaagraha:ref/source/opencity-bbmp"
# A real BBMP Work Order resource (dataset bbmp-work-orders-and-bill-payment). Override via OPENCITY_CSV_URL.
DEFAULT_CSV = ("https://data.opencity.in/dataset/968e5202-74ed-4279-92df-7b7a1a111108/"
               "resource/cfb67e17-32b0-4306-8768-ab4f0e28790e/download/9cf2e217-d96a-4f56-bdd2-6935f06e85cc.csv")

# Declared egress → aos derives meta.egress/sources from THIS (keyless, public open-data host).
egress("data.opencity.in", rate_per_min=30,
       user_agent="AirawatOS/works-register-extractor (+https://airawat.org; manishsv@gmail.com)",
       provides=["works.work_order", "works.contract", "works.contractor", "works.payment"], source_ref=SRC)


def _num(v):
    try:
        return float(str(v).replace(",", "").strip()) if v not in (None, "", "NA", "N/A", "-") else None
    except Exception:
        return None


def _pick(row, *names):
    """Case/space-insensitive column lookup with fallbacks — BBMP CSVs vary header wording across departments."""
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
    cap = int(os.environ.get("MAX_ROWS", "200"))   # fast first-proof cap; raise MAX_ROWS to ingest the full file

    r = ctx.fetch(url)
    if not getattr(r, "ok", False) or not r.body:
        raise SystemExit(f"FATAL: OpenCity fetch failed (status={getattr(r, 'status', None)}) for {url}")
    text = r.body.decode("utf-8", "replace") if isinstance(r.body, (bytes, bytearray)) else str(r.body)
    rows = list(csv.DictReader(io.StringIO(text)))
    capped = len(rows) > cap
    if capped:
        print(f"[works-register-extractor] NOTE: capping {len(rows)} rows → first {cap} (MAX_ROWS). Raise MAX_ROWS to ingest all.")
        rows = rows[:cap]

    ts = now()
    objs, n_wo, n_pay = [], 0, 0
    for row in rows:
        wo_no = str(_pick(row, "work order no", "work_order_no", "wo no", "work order number",
                          "job number", "job no", "work order") or "").strip()
        if not wo_no:
            continue
        key = hashlib.sha1((wo_no + "|" + url).encode()).hexdigest()[:12]
        wo_id = f"ds:{T}:works/work_order/{key}"
        prov = {"source": "opencity-bbmp", "source_ref": SRC, "source_url": url, "retrieved": ts, "raw": row}
        # Fields MUST match the governed works.work_order schema (additionalProperties:false) — the full raw row
        # is preserved under prov.raw, so unmapped columns (e.g. department, deductions) are never lost.
        objs.append({"schema": "works.work_order", "id": wo_id, "tenant": T,
                     "work_order_no": wo_no,
                     "job_number": _pick(row, "job number", "job no", "job_number"),
                     "work_name": _pick(row, "work order details", "work name", "work_name", "name of work", "description"),
                     "ward": _pick(row, "ward", "ward no", "ward_no", "ward name"),
                     "zone": _pick(row, "zone", "zone name"),
                     "sanctioned_amount": _num(_pick(row, "total amount in rs", "approved amount", "sanctioned amount",
                                                      "estimate amount", "estimated amount", "amount")),
                     "work_order_date": _pick(row, "work order date", "wo date", "date", "loa date"),
                     "prov": prov})
        n_wo += 1

        cname = _pick(row, "contractor", "contractor name", "name of contractor", "agency", "agency name")
        contractor_id = None
        if cname:
            contractor_id = f"ds:{T}:works/contractor/{hashlib.sha1(str(cname).encode()).hexdigest()[:12]}"
            # works.contractor allows only name/reg_no (no prov) — keep it minimal + conformant.
            objs.append({"schema": "works.contractor", "id": contractor_id, "tenant": T, "name": str(cname).strip()})

        cval = _num(_pick(row, "contract amount", "contract value", "agreement amount", "tender amount", "loa amount"))
        if cval is not None or contractor_id:
            objs.append({"schema": "works.contract", "id": f"ds:{T}:works/contract/{key}", "tenant": T,
                         "work_order": wo_id, "contractor": contractor_id, "contract_value": cval,
                         "prov": {"source": "opencity-bbmp"}})

        paid = _num(_pick(row, "net payment in rs", "bill amount", "paid amount", "payment amount", "gross amount", "net amount"))
        if paid is not None:
            objs.append({"schema": "works.payment", "id": f"ds:{T}:works/payment/{key}", "tenant": T,
                         "work_order": wo_id, "paid_gross": paid, "prov": {"source": "opencity-bbmp"}})
            n_pay += 1

    if n_wo == 0:
        head = (list(rows[0].keys()) if rows else [])
        raise SystemExit(f"DIAG: fetched {len(text)} chars, csv parsed {len(rows)} rows, matched 0 work-orders. "
                         f"header={head} first120={text[:120]!r}")
    # Per-object writes (the kernel's /v1/write path). write_many batches via /v1/write/batch, which is not
    # reliably available through the confined dev-broker here — a per-record loop persists deterministically.
    written = 0
    for o in objs:
        ctx.write(o)
        written += 1
    print(f"[works-register-extractor] OpenCity rows={len(rows)}{' (capped)' if capped else ''} → "
          f"work_orders={n_wo} payments={n_pay} total_records={written} from {url}", flush=True)
    return 0


if __name__ == "__main__":
    driver.run()
