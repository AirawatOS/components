#!/usr/bin/env python3
"""Fund-Flow Engine — the public-finance lifecycle on top of the double-entry settlement seam.

Two capabilities the HRT scenarios need and the finance core didn't have:
  ADVANCES + LIQUIDATION (S3, Indicator 4): money can be disbursed as an ADVANCE — a receivable, not yet an
    expense. It is later LIQUIDATED (settled against expenditure proof) and RECONCILED. `outstanding = amount −
    liquidated` is the unliquidated balance, and `advance-outstanding` is the "received-but-not-accounted-for"
    watch-list.  Accounting: disburse = Dr Advances(asset) / Cr Cash(asset);  liquidate = Dr Expense / Cr Advances.
  MULTI-STAGE FUND-FLOW (S1): `flow-stamp` records how much of a fund reached each lifecycle stage
    (approved→allocated→released→committed→paid→expended); `flow-status` reports the stage amounts and the
    BOTTLENECK — the first stage where money arrived but didn't move on.

Every ledger posting is delegated to finance-engine (`finance-post`) so the books stay balanced and attributed.
Handlers: advance-disburse · advance-liquidate · advance-reconcile · advance-outstanding · flow-stamp · flow-status.
"""
import time as _t, hashlib as _h, re as _re

FIN = "ds:airawatdeveloper:engine/finance-engine"
STAGES = ["approved", "allocated", "released", "committed", "paid", "expended"]


def _now():
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())


def _slug(s):
    return _re.sub(r"[^a-z0-9]+", "-", str(s or "na").lower()).strip("-") or "na"


def _age_days(iso):
    try:
        return round((_t.time() - _t.mktime(_t.strptime((iso or "")[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400.0, 1)
    except Exception:
        return None


def _acct(ctx, want_type, name_hint=None, override=None):
    """Resolve a GL account by explicit override → name match within type → first of type (by code)."""
    if override:
        return override
    accts = ctx["kfind"]("finance.account")
    if name_hint:
        for a in accts:
            if a.get("type") == want_type and name_hint.lower() in (a.get("name", "") or "").lower():
                return a["id"]
    return next((a["id"] for a in sorted(accts, key=lambda z: z.get("code", "")) if a.get("type") == want_type), None)


# ---- governed fund source (donor topology + on/off-budget) ----------------------------------------
FUNDER_TYPES = ["development_partner", "government", "own_revenue", "ngo", "private", "other"]
MODALITIES = ["on_budget", "off_budget"]


def _fs_id(ctx, key):
    return f"ds:{ctx['tenant']}:finance/fund_source/{_slug(key)}"


# ---- multi-stage fund-flow -------------------------------------------------------------------------
def _flow_id(ctx, project, channel):
    return f"ds:{ctx['tenant']}:finance/fund_flow/{_slug(project)}-{channel}"


def _stamp(ctx, project, channel, stage, amount, by, add=False, fund_source=None):
    fid = _flow_id(ctx, project, channel)
    ff = ctx["kget"](fid) or {"schema": "finance.fund_flow", "id": fid, "tenant": ctx["tenant"],
                              "channel": channel, "project": project, "history": [], "created_at": _now()}
    if fund_source:
        ff["fund_source"] = fund_source                     # the governed finance.fund_source this flow draws on
    cur = float(ff.get(stage) or 0)
    ff[stage] = round(cur + float(amount), 2) if add else round(float(amount), 2)
    ff.setdefault("history", []).append({"stage": stage, "amount": float(amount), "at": _now(), "by": by})
    vals = [(s, float(ff.get(s) or 0)) for s in STAGES]
    nz = [s for s, v in vals if v > 0]
    ff["current_stage"] = nz[-1] if nz else None
    btl, peak = None, 0.0
    for s, v in vals:                                        # bottleneck = first stage below the upstream peak
        peak = max(peak, v)
        if peak > 0 and v < peak - 0.005:
            btl = s
            break
    ff["bottleneck"] = btl
    ff["updated_at"] = _now(); ff["observed_at"] = _now()
    ctx["kwrite"](fid, {k: v for k, v in ff.items() if v is not None})
    return ff


def h_flow_stamp(ctx):
    b = ctx["body"]; project = b.get("project"); stage = b.get("stage"); amount = b.get("amount")
    if not project or stage not in STAGES or not isinstance(amount, (int, float)):
        return {"error": f"project + stage∈{STAGES} + numeric amount required"}, None
    ff = _stamp(ctx, project, b.get("channel") or "government", stage, amount, ctx.get("sub"), add=bool(b.get("add")), fund_source=b.get("fund_source"))
    return {"fund_flow": ff["id"], "current_stage": ff.get("current_stage"), "bottleneck": ff.get("bottleneck"),
            "stages": {s: ff.get(s, 0) for s in STAGES}}, None


def h_flow_status(ctx):
    b = ctx["body"]; where = {}
    if b.get("project"):
        where["project"] = b["project"]
    if b.get("channel"):
        where["channel"] = b["channel"]
    rows = ctx["kfind"]("finance.fund_flow", where or None)
    out = [{"id": f["id"], "project": f.get("project"), "channel": f.get("channel"),
            "stages": {s: f.get(s, 0) for s in STAGES}, "current_stage": f.get("current_stage"),
            "bottleneck": f.get("bottleneck")} for f in rows]
    return {"flows": out, "count": len(out)}, None


# ---- advances + liquidation ------------------------------------------------------------------------
def h_advance_disburse(ctx):
    b, tenant, sub = ctx["body"], ctx["tenant"], ctx.get("sub")
    amount = b.get("amount")
    if not isinstance(amount, (int, float)) or amount <= 0:
        return {"error": "positive amount required"}, None
    adv_acct = _acct(ctx, "asset", "advance", b.get("advance_account"))
    cash_acct = _acct(ctx, "asset", "cash", b.get("cash_account")) or _acct(ctx, "asset", "bank", b.get("cash_account"))
    if not adv_acct or not cash_acct or adv_acct == cash_acct:
        return {"error": "need two distinct asset accounts — an Advances-Receivable and a Cash/Bank "
                         "(seed them, or pass advance_account + cash_account)"}, None
    res, err = ctx["call"](FIN, "finance-post", {                # Dr Advances / Cr Cash — money out as a receivable
        "lines": [{"account": adv_acct, "debit": amount, "credit": 0},
                  {"account": cash_acct, "debit": 0, "credit": amount}],
        "direction": "advance", "memo": b.get("memo") or f"advance to {b.get('to')}", "project": b.get("project"),
        "source": b.get("source"), "source_kind": b.get("source_kind") or "finance.advance", "status": "posted"})
    if err:
        return {"error": f"disbursement posting failed: {err}"}, None
    aid = b.get("id") or f"ds:{tenant}:finance/advance/{_h.sha1((str(b.get('to')) + _now()).encode()).hexdigest()[:12]}"
    adv = {"schema": "finance.advance", "id": aid, "tenant": tenant, "amount": round(float(amount), 2),
           "to_party": b.get("to"), "purpose": b.get("purpose"), "memo": b.get("memo"), "project": b.get("project"),
           "program": b.get("program"), "budget": b.get("budget"), "source": b.get("source"),
           "source_kind": b.get("source_kind"), "channel": b.get("channel"), "fund_source": b.get("fund_source"), "advance_account": adv_acct,
           "disbursed_at": _now(), "disbursed_by": sub, "disbursement_txn": res.get("posted"),
           "utilized": 0.0, "liquidated": 0.0, "outstanding": round(float(amount), 2), "status": "disbursed",
           "liquidations": [], "created_at": _now(), "observed_at": _now()}
    ctx["kwrite"](aid, {k: v for k, v in adv.items() if v is not None})
    if b.get("project"):                                         # a disbursement is money 'released' down the ladder
        _stamp(ctx, b.get("project"), b.get("channel") or "government", "released", amount, sub, add=True, fund_source=b.get("fund_source"))
    return {"advance": aid, "amount": adv["amount"], "outstanding": adv["outstanding"],
            "status": "disbursed", "transaction": res.get("posted")}, None


def h_advance_liquidate(ctx):
    b, tenant, sub = ctx["body"], ctx["tenant"], ctx.get("sub")
    aid = b.get("advance"); adv = ctx["kget"](aid) if aid else None
    if not adv:
        return {"error": f"advance '{aid}' not found"}, None
    outstanding = round(float(adv.get("outstanding") or 0), 2)
    amt = b.get("amount")
    amt = outstanding if amt is None else round(min(float(amt), outstanding), 2)
    if amt <= 0:
        return {"error": "nothing outstanding to liquidate", "outstanding": outstanding}, None
    exp_acct = _acct(ctx, "expense", None, b.get("expense_account"))
    if not exp_acct:
        return {"error": "no expense account to charge (pass expense_account)"}, None
    post = {"lines": [{"account": exp_acct, "debit": amt, "credit": 0},          # Dr Expense / Cr Advances
                      {"account": adv["advance_account"], "debit": 0, "credit": amt}],
            "direction": "expenditure", "memo": b.get("memo") or f"liquidate advance {aid}",
            "project": adv.get("project"), "source": aid, "source_kind": "finance.advance", "status": "posted"}
    if b.get("budget"):                                          # optional budget control at recognition
        post["budget"] = b["budget"]
    res, err = ctx["call"](FIN, "finance-post", post)
    if err:
        return {"error": f"liquidation posting failed: {err}"}, None
    adv["liquidated"] = round(float(adv.get("liquidated") or 0) + amt, 2)
    adv["utilized"] = round(float(adv.get("utilized") or 0) + amt, 2)
    adv["outstanding"] = round(float(adv.get("amount") or 0) - adv["liquidated"], 2)
    adv.setdefault("liquidations", []).append({"amount": amt, "txn": res.get("posted"),
                                               "proof_ref": b.get("proof_ref"), "at": _now()})
    adv["status"] = "liquidated" if adv["outstanding"] <= 0.005 else "partially_liquidated"
    adv["observed_at"] = _now()
    ctx["kwrite"](aid, adv)
    if adv.get("project"):
        _stamp(ctx, adv["project"], adv.get("channel") or "government", "expended", amt, sub, add=True)
    return {"advance": aid, "liquidated_now": amt, "total_liquidated": adv["liquidated"],
            "outstanding": adv["outstanding"], "status": adv["status"], "transaction": res.get("posted")}, None


def h_advance_reconcile(ctx):
    b = ctx["body"]; aid = b.get("advance"); adv = ctx["kget"](aid) if aid else None
    if not adv:
        return {"error": "advance not found"}, None
    if round(float(adv.get("outstanding") or 0), 2) > 0.005:
        return {"error": "cannot reconcile: outstanding balance remains", "outstanding": adv.get("outstanding")}, None
    adv["status"] = "reconciled"; adv["observed_at"] = _now(); ctx["kwrite"](aid, adv)
    return {"advance": aid, "status": "reconciled"}, None


def h_advance_outstanding(ctx):
    b = ctx["body"]; where = {}
    for k in ("project", "program", "to_party"):
        if b.get(k):
            where[k] = b[k]
    rows = [a for a in ctx["kfind"]("finance.advance", where or None) if round(float(a.get("outstanding") or 0), 2) > 0.005]
    rows.sort(key=lambda a: float(a.get("outstanding") or 0), reverse=True)
    items = [{"advance": a["id"], "to_party": a.get("to_party"), "project": a.get("project"),
              "amount": a.get("amount"), "liquidated": a.get("liquidated"), "outstanding": a.get("outstanding"),
              "status": a.get("status"), "age_days": _age_days(a.get("disbursed_at"))} for a in rows]
    return {"outstanding_total": round(sum(float(a.get("outstanding") or 0) for a in rows), 2),
            "count": len(items), "advances": items}, None


# ---- governed fund-source catalog (the two donor topologies, as data) -----------------------------
def h_fund_source(ctx):
    """Upsert a governed finance.fund_source — the explicit 'fund source': funder + funder_type +
    budget_modality (on/off-budget) + recipient_type. Makes the donor topologies data:
      DP → Sector Ministry → Region  (on_budget)   ·   DP → Implementing Partner  (off_budget).
    Attributed component://fund-flow-engine so it satisfies the data-plane writer rule."""
    b = ctx["body"]; ft = b.get("funder_type"); bm = b.get("budget_modality")
    if ft not in FUNDER_TYPES or bm not in MODALITIES:
        return {"error": f"funder_type∈{FUNDER_TYPES} + budget_modality∈{MODALITIES} required"}, None
    key = b.get("id") or b.get("name") or b.get("funder")
    if not key:
        return {"error": "id, name or funder required"}, None
    fid = key if str(key).startswith("ds:") else _fs_id(ctx, key)
    prev = ctx["kget"](fid) or {}
    rec = {"schema": "finance.fund_source", "id": fid, "tenant": ctx["tenant"],
           "name": b.get("name") or prev.get("name") or b.get("funder"),
           "funder": b.get("funder") or prev.get("funder"), "funder_type": ft, "budget_modality": bm,
           "recipient_type": b.get("recipient_type") or prev.get("recipient_type"),
           "programme": b.get("programme") or prev.get("programme"),
           "currency": b.get("currency") or prev.get("currency"),
           "external_ref": b.get("external_ref") or prev.get("external_ref"),
           "steward_role": b.get("steward_role") or prev.get("steward_role") or "finance.programme-manager",
           "created_at": prev.get("created_at") or _now(), "observed_at": _now()}
    ctx["kwrite"](fid, {k: v for k, v in rec.items() if v is not None})
    return {"fund_source": fid, "funder_type": ft, "budget_modality": bm}, None


def h_fund_sources(ctx):
    """List the governed fund sources in this tenant — the catalog the console + flows/advances reference."""
    rows = ctx["kfind"]("finance.fund_source")
    return {"fund_sources": [{k: r.get(k) for k in ("id", "name", "funder", "funder_type", "budget_modality",
            "recipient_type", "programme", "currency", "external_ref", "steward_role")} for r in rows],
            "count": len(rows)}, None


# ---- HRT watchtower: the four core indicators in one read ------------------------------------------
def h_hrt_summary(ctx):
    """Assemble the four HRT indicators + fund-flow bottlenecks + the outstanding-advance watch-list from this
    tenant's finance.budget / finance.fund_flow / finance.advance. One read the Resource-Tracking Console renders."""
    tenant = ctx["tenant"]
    budgets = ctx["kfind"]("finance.budget"); flows = ctx["kfind"]("finance.fund_flow"); advances = ctx["kfind"]("finance.advance")
    def SUM(rows, k):
        return round(sum(float(r.get(k) or 0) for r in rows), 2)
    alloc = SUM(budgets, "allocated"); spent = SUM(budgets, "spent"); committed = SUM(budgets, "committed")
    crev = SUM(budgets, "committed_revenue"); rrev = SUM(budgets, "realized_revenue")
    stage_tot = {s: round(sum(float(f.get(s) or 0) for f in flows), 2) for s in STAGES}
    # Indicator 1 — envelope by GOVERNED fund source (finance.fund_source): funder + on/off-budget topology.
    sources = ctx["kfind"]("finance.fund_source"); by_id = {s["id"]: s for s in sources}
    def _src_of(rec):                                       # the fund_source a flow/advance draws on (or None)
        return by_id.get(rec.get("fund_source")) if rec.get("fund_source") else None
    _CH_FUNDER = {"government": "Government", "partner": "Development Partner", "grant": "Grant"}
    _CH_FTYPE = {"partner": "development_partner", "grant": "development_partner", "government": "government"}
    by_modality = {"on_budget": 0.0, "off_budget": 0.0, "unclassified": 0.0}
    by_funder, by_ftype = {}, {}
    for f in flows:                                         # envelope = 'approved' at the top of each flow
        amt = float(f.get("approved") or 0)
        if amt <= 0.005:
            continue
        s = _src_of(f); mod = (s or {}).get("budget_modality") or "unclassified"
        by_modality[mod] = round(by_modality.get(mod, 0.0) + amt, 2)
        funder = (s or {}).get("funder") or _CH_FUNDER.get(f.get("channel"), "Unclassified")
        ftype = (s or {}).get("funder_type") or _CH_FTYPE.get(f.get("channel"), "government")
        bf = by_funder.setdefault(funder, {"funder": funder, "funder_type": ftype, "budget_modality": (s or {}).get("budget_modality"),
                                           "fund_source": (s or {}).get("id"), "recipient_type": (s or {}).get("recipient_type"),
                                           "approved": 0.0, "released": 0.0, "expended": 0.0})
        bf["approved"] = round(bf["approved"] + amt, 2)
        bf["released"] = round(bf["released"] + float(f.get("released") or 0), 2)
        bf["expended"] = round(bf["expended"] + float(f.get("expended") or 0), 2)
        by_ftype[ftype] = round(by_ftype.get(ftype, 0.0) + amt, 2)
    env_total = round(by_modality["on_budget"] + by_modality["off_budget"] + by_modality["unclassified"], 2) or round(max(alloc - crev, 0) + crev, 2)
    by_source = {"by_modality": {k: v for k, v in by_modality.items() if v > 0.005 or k != "unclassified"},
                 "by_funder_type": by_ftype,
                 "by_funder": sorted(by_funder.values(), key=lambda z: -z["approved"]),
                 "catalog": [{k: s.get(k) for k in ("id", "name", "funder", "funder_type", "budget_modality",
                              "recipient_type", "programme", "steward_role")} for s in sources]}
    out = [a for a in advances if round(float(a.get("outstanding") or 0), 2) > 0.005]
    watch = [{"advance": a["id"], "to_party": a.get("to_party"), "project": a.get("project"),
              "amount": a.get("amount"), "liquidated": a.get("liquidated"), "outstanding": a.get("outstanding"),
              "status": a.get("status"), "age_days": _age_days(a.get("disbursed_at"))}
             for a in sorted(out, key=lambda z: -float(z.get("outstanding") or 0))]
    return {"tenant": tenant,
            "envelope": {"total": env_total, "by_source": by_source},
            "allocation": {"allocated": alloc, "committed": committed,
                           "by_budget": [{"budget": b.get("name") or b.get("id"), "allocated": float(b.get("allocated") or 0),
                                          "spent": float(b.get("spent") or 0)} for b in budgets]},
            "execution": {"spent": spent, "allocated": alloc, "rate_pct": (round(100.0 * spent / alloc, 1) if alloc else None)},
            "outstanding": {"disbursed": SUM(advances, "amount"), "liquidated": SUM(advances, "liquidated"),
                            "outstanding": round(sum(float(a.get("outstanding") or 0) for a in out), 2),
                            "released": stage_tot.get("released"), "expended": stage_tot.get("expended"), "count": len(out)},
            "stages": stage_tot,
            "bottlenecks": [{"project": f.get("project"), "channel": f.get("channel"), "bottleneck": f.get("bottleneck"),
                             "current_stage": f.get("current_stage"), "stages": {s: f.get(s, 0) for s in STAGES}} for f in flows],
            "watchlist": watch, "revenue": {"committed": crev, "realized": rrev}}, None


HANDLERS = {
    "advance-disburse": h_advance_disburse, "advance-liquidate": h_advance_liquidate,
    "advance-reconcile": h_advance_reconcile, "advance-outstanding": h_advance_outstanding,
    "flow-stamp": h_flow_stamp, "flow-status": h_flow_status, "hrt-summary": h_hrt_summary,
    "fund-source": h_fund_source, "fund-sources": h_fund_sources,
}


def main():
    print("Fund-Flow Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
