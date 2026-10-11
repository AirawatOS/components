#!/usr/bin/env python3
"""tender-flags — procurement transparency flags for the Public Expenditure Dashboard.

Reads the governed procurement.tender facts and writes governance.recommendation records
("AI flags, humans decide", ADR-0029) — it never mutates data or auto-acts; each flag cites its
evidence (example tender references + counts) and a human reviews it. No egress; governed reads + writes.

Flags produced (systemic, aggregate — competition & completion signals that the source data makes visible):
  1. Single recorded bid — awards/evaluations with only one bid on record = no price competition.
  2. Recalled tenders.
  3. Retendered tenders (a first attempt that did not result in an award).
  4. No-bids tenders.
Generic: no agency/city is hardcoded; thresholds are config. Scope is whatever procurement.tender holds
(BBMP road tenders in the first instance).
"""
import hashlib
from aos import driver, ctx, now

PRODUCER = "ds:janaagraha:driver/tender-flags"

def _obj(r):
    return r.get("object", r) if isinstance(r, dict) else r

def _rec(title, action, evidence, rationale, confidence):
    rid = f"ds:{ctx.tenant}:governance/recommendation/flag-{hashlib.sha1(title.encode()).hexdigest()[:16]}"
    ctx.write({"schema": "governance.recommendation", "id": rid, "tenant": ctx.tenant,
               "title": title[:200], "produced_by": PRODUCER, "method": "rule",
               "proposed_action": action, "rationale": rationale,
               "evidence": [e for e in (evidence or []) if e], "confidence": confidence,
               "created_at": now()})

def _pct(a, b):
    return (100.0 * a / b) if b else 0.0

@driver.main
def run():
    rows = [_obj(r) for r in ctx.facts("procurement.tender")]
    n = len(rows)
    if not n:
        print("[tender-flags] no procurement.tender rows — nothing to flag", flush=True); return
    def status(t): return (t.get("status") or "").strip().lower()
    single = [t for t in rows if t.get("num_bidders") == 1]
    recalled = [t for t in rows if "recall" in status(t)]
    retender = [t for t in rows if "retender" in status(t) or "re-tender" in status(t)]
    nobids = [t for t in rows if "no bid" in status(t)]
    def ex(ts, k=5):
        return ["tender " + (t.get("tender_ref") or "?") + (" — " + (t.get("title") or "")[:60] if t.get("title") else "") for t in ts[:k]]

    flags = 0
    if single:
        _rec(
            f"Single recorded bid in {_pct(len(single),n):.0f}% of tenders — limited price competition",
            "Review single-bid awards for recurring firms and tender terms/timelines that may suppress competition.",
            ex(single),
            f"{len(single):,} of {n:,} tenders ({_pct(len(single),n):.1f}%) record only a single bid. A lone bid means "
            f"no competitive pressure on price; recurring single-bid awards to the same contractors warrant scrutiny. "
            f"(Source records at most one bidder per tender, so this counts tenders with a bid on record and no competitor.)",
            0.9); flags += 1
    if retender:
        _rec(
            f"{len(retender):,} tenders Retendered ({_pct(len(retender),n):.0f}%) — first attempt did not result in an award",
            "Check why these works were re-tendered (pricing, eligibility, timelines) and whether delays followed.",
            ex(retender),
            f"{len(retender):,} of {n:,} tenders ({_pct(len(retender),n):.1f}%) carry a Retendered status, i.e. an "
            f"earlier call did not produce an award. Frequent re-tendering delays delivery and can signal mis-pricing "
            f"or restrictive terms.",
            0.85); flags += 1
    if recalled:
        _rec(
            f"{len(recalled):,} tenders Recalled ({_pct(len(recalled),n):.0f}%)",
            "Review recalled tenders for the reason and whether the same work was re-advertised on changed terms.",
            ex(recalled),
            f"{len(recalled):,} of {n:,} tenders ({_pct(len(recalled),n):.1f}%) were Recalled after publication. "
            f"Recalls can be legitimate corrections, but a high rate — or recall-then-re-advertise patterns — merits review.",
            0.85); flags += 1
    if nobids:
        _rec(
            f"{len(nobids):,} tenders received No Bids",
            "Check whether no-bid works were dropped, re-tendered, or awarded by another route.",
            ex(nobids),
            f"{len(nobids):,} of {n:,} tenders ({_pct(len(nobids),n):.1f}%) received no bids at all. Persistent no-bids "
            f"on a category of work can indicate unattractive terms, payment-delay reputation, or scoping problems.",
            0.8); flags += 1

    print(f"[tender-flags] {n} tenders → wrote {flags} governance.recommendation flags "
          f"(single-bid={len(single)}, retender={len(retender)}, recalled={len(recalled)}, no-bids={len(nobids)})", flush=True)

if __name__ == "__main__":
    driver.run()
