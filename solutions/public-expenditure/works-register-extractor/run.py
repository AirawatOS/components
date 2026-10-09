#!/usr/bin/env python3
"""works-register-extractor — BBMP public work-orders from the OpenCity open dataset (ODbL) → governed
public-works facts for the Public Expenditure Dashboard. One CSV row → works.work_order (+ works.contractor,
works.contract, works.payment), each carrying full provenance back to the source CSV.

aos SDK (clean-room / ADR-0030): the ONLY I/O is the broker. The OpenCity CSV is fetched via ctx.fetch
(brokered, declared egress), governed records are written via ctx.write, the write-tenant is ctx.tenant.
No raw sockets — so it runs identically in the dev sandbox (Studio Test, L1) and when installed.

REAL OpenCity data only: a fetch error fails loudly; ZERO work-order rows is a legitimate empty result
(reported, exit 0, nothing invented). The full raw CSV row is preserved under prov.raw (original-input
preservation) so no source field is ever lost, even when a column name isn't recognised.

AMOUNTS: this file's money columns (Total/Deduction/Net) are inconsistent and the file is denormalised, so
rupee amounts are deliberately NOT mapped — we land the reliable INVENTORY (work + contractor + contract +
dates) and record only the bill-line count. Spend should come from eProc award values / BBMP IFMS.

  Env: OPENCITY_CSV_URL (a BBMP "Work Order Data" resource CSV; default = 308-CE-Lakes 2015-18) ·
       MAX_ROWS (optional cap for a quick run; default 0 = ingest the WHOLE file; a cap is LOGGED).
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
    cap = int(os.environ.get("MAX_ROWS", "0") or 0)   # 0 = ingest the WHOLE file; set MAX_ROWS to cap for a quick run

    r = ctx.fetch(url)
    if not getattr(r, "ok", False) or not r.body:
        raise SystemExit(f"FATAL: OpenCity fetch failed (status={getattr(r, 'status', None)}) for {url}")
    text = r.body.decode("utf-8", "replace") if isinstance(r.body, (bytes, bytearray)) else str(r.body)
    rows = list(csv.DictReader(io.StringIO(text)))
    capped = bool(cap) and len(rows) > cap          # MAX_ROWS=0 (default) ⇒ ingest the WHOLE file
    if capped:
        print(f"[works-register-extractor] NOTE: MAX_ROWS={cap} — ingesting first {cap} of {len(rows)} rows.")
        rows = rows[:cap]

    ts = now()
    # AMOUNT SEMANTICS — deliberately NOT summed. This "Work Orders & Bill Payment" file is heavily
    # DENORMALISED (6000 rows for ~37 work orders; one order had 1,214 rows) and its money columns are not
    # self-consistent (Deduction and Net Payment both exceed Total Amount), with no published data dictionary.
    # So no arithmetic on it yields a trustworthy rupee figure. We therefore land the RELIABLE part — the
    # work-order INVENTORY (work + contractor + contract link + dates) — and leave sanctioned_amount /
    # contract_value / paid_* EMPTY, recording only the bill-line COUNT. The full raw row stays in prov.raw.
    # Reliable spend must come from a clean source (Karnataka eProc award values / BBMP IFMS payments).
    wo, ct, cr, pay = {}, {}, {}, {}
    for row in rows:
        wo_no = str(_pick(row, "work order number", "work order no", "wo no", "work_order_no",
                          "job number", "job no", "work order") or "").strip()
        if not wo_no:
            continue
        key = hashlib.sha1((wo_no + "|" + url).encode()).hexdigest()[:12]
        wo_id = f"ds:{T}:works/work_order/{key}"
        if wo_id not in wo:
            wo[wo_id] = {"schema": "works.work_order", "id": wo_id, "tenant": T, "work_order_no": wo_no,
                         "job_number": _pick(row, "job number", "job no", "job_number"),
                         "work_name": _pick(row, "work order details", "work name", "work_name", "name of work", "description"),
                         "ward": _pick(row, "ward", "ward no", "ward_no", "ward name"),
                         "zone": _pick(row, "zone", "zone name"),
                         "work_order_date": _pick(row, "work order date", "wo date", "date", "loa date"),
                         # sanctioned_amount OMITTED — source amount columns are not interpretable (see prov.amounts_note)
                         "prov": {"source": "opencity-bbmp", "source_ref": SRC, "source_url": url, "retrieved": ts, "raw": row,
                                  "amounts_note": "source Total/Deduction/Net columns are inconsistent and the file is "
                                                  "denormalised; rupee amounts intentionally NOT mapped — use eProc/IFMS for spend"}}

        cname = _pick(row, "contractor", "contractor name", "name of contractor", "agency", "agency name")
        contractor_id = None
        if cname:
            contractor_id = f"ds:{T}:works/contractor/{hashlib.sha1(str(cname).encode()).hexdigest()[:12]}"
            cr.setdefault(contractor_id, {"schema": "works.contractor", "id": contractor_id, "tenant": T, "name": str(cname).strip()})

        cid = f"ds:{T}:works/contract/{key}"
        if cid not in ct:
            # contract_value OMITTED (same reason); the work_order↔contractor LINK is the reliable part.
            ct[cid] = {"schema": "works.contract", "id": cid, "tenant": T, "work_order": wo_id,
                       "contractor": contractor_id, "prov": {"source": "opencity-bbmp"}}

        pid = f"ds:{T}:works/payment/{key}"
        p = pay.setdefault(pid, {"schema": "works.payment", "id": pid, "tenant": T, "work_order": wo_id,
                                 # paid_net / paid_gross OMITTED — only the count of bill lines is trustworthy here
                                 "prov": {"source": "opencity-bbmp", "source_url": url, "bill_lines": 0,
                                          "amounts_note": "bill-line count only; source rupee amounts not interpretable"}})
        p["prov"]["bill_lines"] += 1

    if not wo:
        head = (list(rows[0].keys()) if rows else [])
        raise SystemExit(f"DIAG: parsed {len(rows)} rows, matched 0 work-orders. header={head}")
    objs = list(wo.values()) + list(cr.values()) + list(ct.values()) + list(pay.values())
    for o in objs:
        ctx.write(o)
    print(f"[works-register-extractor] OpenCity rows={len(rows)}{' (capped)' if capped else ''} → "
          f"work_orders={len(wo)} contracts={len(ct)} contractors={len(cr)} payments={len(pay)} "
          f"(INVENTORY only — amounts intentionally empty, source not interpretable) total_records={len(objs)} from {url}", flush=True)
    return 0


if __name__ == "__main__":
    driver.run()
