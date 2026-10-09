#!/usr/bin/env python3
"""works-flags — transparency flags for the Public Expenditure Dashboard. Reads the governed works.* facts and
writes governance.recommendation records ("AI flags, humans decide", ADR-0029) — it never mutates the data or
auto-merges anything; each flag cites its evidence and a human reviews it. No egress; governed reads + writes only.

Flags produced:
  1. Contractor name-variant clusters — the same agency recorded under many spellings (e.g. KRIDL / M/s KRIDL /
     kridl / KRIDL9845…), which fragments and understates true vendor concentration. Flags each cluster of ≥2
     spellings with material spend, with the consolidated ₹ and works.
  2. High-deduction outliers — works where deductions are an unusually large share of the gross bill.
"""
import re, hashlib
from aos import driver, ctx, now

PRODUCER = "ds:janaagraha:driver/works-flags"


def _obj(r):
    return r.get("object", r) if isinstance(r, dict) else r


def _rec(title, action, evidence, rationale, confidence):
    """Write a conforming governance.recommendation (ADR-0029) — id is stable on the title so re-runs update
    in place. Matches the governed schema: produced_by is a required string; no observed_at."""
    rid = f"ds:{ctx.tenant}:governance/recommendation/flag-{hashlib.sha1(title.encode()).hexdigest()[:16]}"
    ctx.write({"schema": "governance.recommendation", "id": rid, "tenant": ctx.tenant,
               "title": title[:200], "produced_by": PRODUCER, "method": "rule",
               "proposed_action": action, "rationale": rationale,
               "evidence": [e for e in (evidence or []) if e], "confidence": confidence,
               "created_at": now()})


def _norm(nm):
    s = (nm or "").upper().strip()
    s = re.sub(r"^(M/S|M/S\.|MESSRS|SRI|SMT|MR|MRS)\b[\s.]*", "", s)   # drop honorifics/prefixes
    s = re.sub(r"\d{3,}", " ", s)                                     # drop long digit runs (phone / ids)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)                                 # punctuation → space
    return re.sub(r"\s+", " ", s).strip()


def _famkey(nm):
    n = _norm(nm)
    toks = n.split()
    return toks[0] if (toks and len(toks[0]) >= 4) else n             # leading token (≥4 chars) = the family


@driver.main
def run():
    if not ctx.tenant:
        raise SystemExit("FATAL: TENANT not set.")
    flags = 0

    # ---- 1) contractor name-variant clusters (de-dup candidates) ----
    clusters = {}
    for r in ctx.facts("works.contractor_rollup"):
        o = _obj(r)
        fam = _famkey(o.get("name"))
        if not fam:
            continue
        c = clusters.setdefault(fam, {"names": set(), "ids": [], "works": 0, "net": 0.0})
        c["names"].add((o.get("name") or "").strip())
        if o.get("contractor"):
            c["ids"].append(o["contractor"])
        c["works"] += o.get("works_count") or 0
        c["net"] += o.get("paid_net_total") or 0.0

    for fam, c in sorted(clusters.items(), key=lambda kv: -kv[1]["net"]):
        if len(c["names"]) >= 2 and c["net"] >= 5e7:                  # ≥2 spellings, ≥ ₹5 cr combined
            shown = sorted(c["names"])[:6]
            more = "…" if len(c["names"]) > 6 else ""
            _rec(f"Contractor name variants: {fam} — {len(c['names'])} spellings, ₹{round(c['net']/1e7,1)} cr",
                 "Verify these are the same vendor and consolidate the name variants, so vendor concentration is measured accurately.",
                 c["ids"][:50],
                 (f"{len(c['names'])} spellings map to the same leading name '{fam}': "
                  f"{', '.join(shown)}{more}. Combined: {c['works']} works, ₹{round(c['net']/1e7,1)} cr net paid. "
                  f"Fragmented names understate how much went to one agency."),
                 0.6)
            flags += 1

    # ---- 2) high-deduction outliers (bounded) ----
    wo = {}
    for r in ctx.facts("works.work_order"):
        o = _obj(r); wo[o.get("id")] = (o.get("work_name"), o.get("ward"))
    outliers = []
    for r in ctx.facts("works.payment"):
        p = _obj(r); g = p.get("paid_gross"); n = p.get("paid_net")
        if g and n is not None and g > 0:
            ratio = (g - n) / g
            if ratio >= 0.30 and g >= 1e6:                           # ≥30% deducted on a ≥ ₹10 lakh work
                outliers.append((ratio, g, n, p.get("work_order")))
    outliers.sort(reverse=True)
    for ratio, g, n, woid in outliers[:40]:
        nm, ward = wo.get(woid, (None, None))
        _rec(f"High deduction ({round(ratio*100)}%): {str(nm or woid)[:48]}",
             "Review why deductions are an unusually large share of the gross bill for this work.",
             [woid],
             f"Gross ₹{round(g/1e7,2)} cr, net ₹{round(n/1e7,2)} cr — {round(ratio*100)}% deducted" + (f" (ward {ward})." if ward else "."),
             0.5)
        flags += 1

    print(f"[works-flags] contractor-variant clusters + high-deduction outliers → wrote {flags} governance.recommendation flags", flush=True)
    return 0


if __name__ == "__main__":
    driver.run()
