#!/usr/bin/env python3
"""works-register-extractor — BBMP public works from the OpenCity open datasets (ODbL) → governed public-works
facts for the Public Expenditure Dashboard: works.work_order (+ works.contractor, works.contract, works.payment),
each carrying full provenance back to the source CSV.

aos SDK (clean-room / ADR-0030): the ONLY I/O is the broker. CSVs are fetched via ctx.fetch (brokered,
declared egress); governed records are written via ctx.write_many (batched); the write-tenant is ctx.tenant.
No raw sockets — so it runs identically in the dev sandbox (Studio Test, L1) and when installed.

CITY-WIDE by default. The IFMS "BBMP Work Orders by Ward (2013-2022)" dataset publishes one CSV per ward
(~198). With no OPENCITY_CSV_URL set, this driver DISCOVERS every ward CSV from the CKAN package and ingests
them all — deriving the ward label from each resource name so the dashboard can group spend by ward. Use
WARD_OFFSET / WARD_LIMIT to ingest a slice at a time (keeps each run inside its timeout); set OPENCITY_CSV_URL
to pin a single file.

SELF-VALIDATING AMOUNTS. works.payment carries REAL paid_gross/paid_net, populated ONLY from source bill rows
that pass a per-row consistency gate (gross − deduction == nett). The by-ward IFMS files are internally
consistent, so their money is trustworthy; the denormalised dept-wise register is not, so its amounts stay
withheld (inventory + prov.amounts_note) — ONE source-agnostic driver, honest on both. Join key = the
work-order / job number (dept-FY-serial, e.g. 56-15-000012), the same key across BBMP sources.

REAL data only: a fetch error on the catalogue fails loudly; a single unreadable ward is logged and skipped
(one bad file never aborts a city-wide run); ZERO work-order rows is a legitimate empty result. The full raw
row is preserved under prov.raw.

  Env: OPENCITY_CSV_URL (pin a single CSV; default = discover all wards) · OPENCITY_DATASET (CKAN id;
       default bbmp-work-orders-by-ward-2013-2022) · WARD_OFFSET / WARD_LIMIT (ward-slice for batched runs;
       default 0/0 = all) · WARD (override the ward label for a single-file run) · MAX_ROWS (per-file cap;
       default 0 = whole file).
"""
import csv, io, os, re, time, hashlib
from aos import driver, ctx, egress, now

FETCH_PACE = float(os.environ.get("FETCH_PACE_SEC", "1.7"))   # stay under the broker's egress rate ceiling


def _fetch_paced(url, retries=2):
    """Fetch one CSV, honouring the broker egress rate limit: a small inter-request pace plus a bounded
    retry that respects the 429 'retry after Ns' hint — a city-wide pull is 198 files over one host."""
    for attempt in range(retries + 1):
        r = ctx.fetch(url)
        if getattr(r, "ok", False):
            return r
        body = (r.body.decode("utf-8", "replace") if isinstance(getattr(r, "body", None), (bytes, bytearray)) else str(getattr(r, "body", "") or ""))
        if getattr(r, "status", None) == 429 and attempt < retries:
            m = re.search(r"retry after (\d+)", body)
            time.sleep((int(m.group(1)) if m else 20) + 2)
            continue
        return r

SRC = "ds:janaagraha:ref/source/opencity-bbmp"
DATASET_DEFAULT = "bbmp-work-orders-by-ward-2013-2022"   # IFMS "Work Orders by Ward" — self-consistent amounts
CKAN = "https://data.opencity.in/api/3/action/package_show?id="
WRITE_CHUNK = 800                                        # batch size for ctx.write_many round-trips

egress("data.opencity.in", rate_per_min=120,
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


def _ward_label(name):
    """Resource name → a clean ward label. BBMP names them e.g. 'Work Orders for Hemmigepura(Num-198) Ward'
    or 'Work Orders for Kempegowda Ward(Num-1) W…'. Produce 'Hemmigepura (198)'."""
    if not name:
        return None
    s = re.sub(r"(?i)^work orders? for\s+", "", str(name)).strip()
    num = None
    m = re.search(r"Num[-\s]*(\d+)", s, re.I)
    if m:
        num = m.group(1)
    nm = re.sub(r"(?i)\(.*?\)|ward\b.*$", "", s).strip(" -")
    return f"{nm} ({num})" if (nm and num) else (nm or (f"Ward {num}" if num else None))


def _rows_from_csv(text):
    """Parse a BBMP work-order CSV, tolerating a title-banner preamble before the real header row."""
    lines = text.splitlines()
    hdr = 0
    for i, ln in enumerate(lines[:15]):
        ll = ln.lower()
        if (("wo num" in ll) or ("work order" in ll) or ("job number" in ll)) and \
           (("amount" in ll) or ("nett" in ll) or ("contractor" in ll) or ("details" in ll) or ("deduction" in ll)):
            hdr = i
            break
    return list(csv.DictReader(io.StringIO("\n".join(lines[hdr:]))))


def _ingest(rows, url, ward, T, ts, wo, ct, cr, pay):
    """Fold one file's rows into the running accumulators (keyed per work order)."""
    for row in rows:
        wo_no = str(_pick(row, "wo num", "work order number", "work order no", "work_order_no",
                          "job number", "job no", "wo no", "work order") or "").strip()
        if not wo_no:
            continue
        key = hashlib.sha1((wo_no + "|" + url).encode()).hexdigest()[:12]
        wo_id = f"ds:{T}:works/work_order/{key}"
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
            contractor_id = f"ds:{T}:works/contractor/{hashlib.sha1(str(cname).strip().encode()).hexdigest()[:12]}"
            cr.setdefault(contractor_id, {"schema": "works.contractor", "id": contractor_id, "tenant": T, "name": str(cname).strip()})

        cid = f"ds:{T}:works/contract/{key}"
        if cid not in ct:
            ct[cid] = {"schema": "works.contract", "id": cid, "tenant": T, "work_order": wo_id,
                       "contractor": contractor_id, "prov": {"source": "opencity-bbmp"}}

        # payment: aggregate per work order, ONLY from rows whose amounts self-validate (gross − deduction == net)
        pid = f"ds:{T}:works/payment/{key}"
        p = pay.setdefault(pid, {"_gross": 0.0, "_net": 0.0, "_lines": 0, "_ok": 0})
        gross = _num(_pick(row, "amount", "total amount in rs", "gross", "bill amount"))
        ded = _num(_pick(row, "deduction", "deduction in rs")) or 0.0
        net = _num(_pick(row, "nett", "net payment in rs", "net amount", "paid amount"))
        p["_lines"] += 1
        if gross is not None and net is not None and abs(gross - ded - net) < 1.0:
            p["_gross"] += gross
            p["_net"] += net
            p["_ok"] += 1


def _finalize_payments(pay, T):
    """Turn the per-work-order payment accumulators into works.payment records: trustworthy paid_gross/paid_net
    only when ≥1 bill passed the consistency gate; otherwise amounts are left empty with a prov note."""
    out = []
    for pid, agg in pay.items():
        key = pid.rsplit("/", 1)[-1]
        rec = {"schema": "works.payment", "id": pid, "tenant": T, "work_order": f"ds:{T}:works/work_order/{key}",
               "prov": {"source": "opencity-bbmp", "bill_lines": agg["_lines"]}}
        if agg["_ok"] > 0:
            rec["paid_gross"] = round(agg["_gross"], 2)
            rec["paid_net"] = round(agg["_net"], 2)
            rec["prov"]["consistent_bills"] = agg["_ok"]
        else:
            rec["prov"]["amounts_note"] = "source rupee amounts failed the gross−deduction==net consistency check; not mapped"
        out.append(rec)
    return out


@driver.main
def run():
    if not ctx.tenant:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant.")
    T = ctx.tenant
    single = os.environ.get("OPENCITY_CSV_URL")
    dataset = os.environ.get("OPENCITY_DATASET", DATASET_DEFAULT)
    ward_env = (os.environ.get("WARD") or "").strip() or None
    offset = int(os.environ.get("WARD_OFFSET", "0") or 0)
    limit = int(os.environ.get("WARD_LIMIT", "0") or 0)
    cap = int(os.environ.get("MAX_ROWS", "0") or 0)

    # Build the work-list of (csv_url, ward_label): a single pinned file, or every ward CSV in the dataset.
    if single:
        worklist = [(single, ward_env)]
    else:
        cr0 = _fetch_paced(CKAN + dataset)
        if not getattr(cr0, "ok", False) or not cr0.body:
            raise SystemExit(f"FATAL: CKAN package_show failed (status={getattr(cr0, 'status', None)}) for {dataset}")
        pkg = cr0.json()
        res = [r for r in (pkg.get("result", {}).get("resources") or []) if (r.get("format") or "").lower() == "csv"]
        res.sort(key=lambda r: r.get("name") or "")
        worklist = [(r["url"], _ward_label(r.get("name"))) for r in res if r.get("url")]
        if offset or limit:
            worklist = worklist[offset: (offset + limit) if limit else None]
    if not worklist:
        raise SystemExit(f"DIAG: no CSV resources to ingest (dataset={dataset}, offset={offset}, limit={limit}).")

    ts = now()
    seen_cr = set()                                # contractor ids already written (dedupe writes across wards)
    tot = dict(files_ok=0, files_bad=0, rows=0, wo=0, ct=0, cr=0, pay=0, amt=0, written=0)
    # FLUSH PER WARD: each ward's records are written as soon as the file is parsed, so a long city-wide pull
    # keeps memory bounded AND a timeout only loses the in-flight ward — every completed ward is durable.
    for idx, (url, ward) in enumerate(worklist):
        if idx:
            time.sleep(FETCH_PACE)                 # polite pacing: keep under the broker's per-host egress ceiling
        try:
            r = _fetch_paced(url)
            if not getattr(r, "ok", False) or not r.body:
                tot["files_bad"] += 1
                print(f"[works-register-extractor] WARN skip (status={getattr(r, 'status', None)}) {ward or url}", flush=True)
                continue
            text = r.body.decode("utf-8", "replace") if isinstance(r.body, (bytes, bytearray)) else str(r.body)
            rows = _rows_from_csv(text)
            if cap and len(rows) > cap:
                rows = rows[:cap]
            wo, ct, cr, pay = {}, {}, {}, {}
            _ingest(rows, url, ward, T, ts, wo, ct, cr, pay)
            if not wo:
                tot["files_ok"] += 1; tot["rows"] += len(rows)
                continue
            pay_objs = _finalize_payments(pay, T)
            cr_new = [o for cid, o in cr.items() if cid not in seen_cr]
            seen_cr.update(cr.keys())
            objs = list(wo.values()) + cr_new + list(ct.values()) + pay_objs
            for i in range(0, len(objs), WRITE_CHUNK):
                tot["written"] += ctx.write_many(objs[i:i + WRITE_CHUNK])
            tot["files_ok"] += 1; tot["rows"] += len(rows)
            tot["wo"] += len(wo); tot["ct"] += len(ct); tot["cr"] += len(cr_new); tot["pay"] += len(pay_objs)
            tot["amt"] += sum(1 for p in pay_objs if "paid_net" in p)
        except Exception as e:
            tot["files_bad"] += 1
            print(f"[works-register-extractor] WARN error on {ward or url}: {str(e)[:120]}", flush=True)

    if tot["wo"] == 0:
        raise SystemExit(f"DIAG: ingested {tot['files_ok']} file(s), {tot['rows']} rows, matched 0 work-orders.")
    print(f"[works-register-extractor] wards={tot['files_ok']} (skipped {tot['files_bad']}) rows={tot['rows']} → "
          f"work_orders={tot['wo']} contracts={tot['ct']} contractors={tot['cr']} payments={tot['pay']} "
          f"({tot['amt']} with trustworthy amounts) wrote={tot['written']} records", flush=True)
    return 0


if __name__ == "__main__":
    driver.run()
