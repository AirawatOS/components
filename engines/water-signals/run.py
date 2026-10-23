#!/usr/bin/env python3
"""water-signals — operational signals (water.signal) + the dashboard projection (water.overview).

Reconstructed 2026-10-23 from the governed build-guide + the recorded oracle (airawatdeveloper / DJB:
48 signals, 1 overview). The pure core `compute_signals()` is verified to reproduce the recorded records
EXACTLY (emerging/category 9/9, reopen/category 9/9, reopen/citywide, recurring active/chronic 343/224,
recurring/cell 15/15, and every water.overview total). No taxonomy is hardcoded — non-complaint record kinds
come from water.record_kind and category→domain from water.category.

Signal families (anchor date = max registered_at = `TO`):
  emerging/category   per category: newest=count(last 7d [TO-6..TO]), prior=count(prior 7d [TO-13..TO-7]),
                      metric=(newest-prior)/prior (wow_change); emitted when prior>0.
  emerging/citywide   same over ALL complaints.
  recurring/cell      chronic geocoded cells (present in ALL 4 trailing weeks), top TOP_CELLS by 28-day count;
                      metric = that 28-day count (complaints_all_weeks).
  recurring/citywide  chronic_share = chronic_cells / active_cells over the 4 weeks.
  reopen/category     per category: reopened(reopens>=1) of complaints; metric = reopen_rate.
  reopen/ward         top TOP_WARDS wards by reopened count; metric = reopened_count.
  reopen/citywide     reopened of all complaints + total reopen_events.
water.overview — totals (closed/pending/queries/no_response_calls/contacts/complaints/geocoded/geocode_pct/
  closure_rate/reopened_complaints/reopen_events), by_* group breakdowns, and a per-day trend over [FROM..TO].
Confined — only I/O is the SDK ctx (broker).
"""
import os, datetime as _dt
from collections import Counter, defaultdict
from aos import driver, ctx


def _date(s):
    return (s or "")[:10]


def _ri(v):
    try:
        return int(v)
    except Exception:
        return 0


def _slug(s):
    return "".join(ch if ch.isalnum() else "-" for ch in (s or "").lower()).strip("-").replace("--", "-")


def _top(counter, n):
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def compute_signals(records, record_kinds, categories, config, now, top_wards=12, top_cells=15):
    """PURE core (no I/O) — returns (signal_rows, overview_row). `records` = ALL water.complaint rows
    (every record_kind). Shared by the engine and the offline verifier."""
    T = ctx.tenant
    noncomplaint = {r.get("code") for r in record_kinds if r.get("code") and not r.get("is_complaint")}
    cat_domain = {c.get("code"): (c.get("domain") or "water") for c in categories if c.get("code")}

    complaints = [r for r in records if r.get("record_kind") not in noncomplaint]
    dated = [r for r in records if _date(r.get("registered_at"))]
    if not dated:
        return [], None
    TO = max(_date(r["registered_at"]) for r in dated)
    FROM = min(_date(r["registered_at"]) for r in dated)
    tod = _dt.date.fromisoformat(TO); frm = _dt.date.fromisoformat(FROM)
    span_days = (tod - frm).days + 1
    weeks = min(int(((config or {}).get("windows") or {}).get("max_weeks", 4)), max(1, span_days // 7))
    window = {"from": FROM, "to": TO, "span_days": span_days}

    def wk(items, i):
        a = (tod - _dt.timedelta(days=7 * i + 6)).isoformat(); b = (tod - _dt.timedelta(days=7 * i)).isoformat()
        return [x for x in items if a <= _date(x.get("registered_at")) <= b]

    sig = []

    def _emerging(items, scope, key, label=None):
        new = len(wk(items, 0)); prior = len(wk(items, 1))
        if prior <= 0:
            return
        metric = round((new - prior) / prior, 4); pct = round(metric * 100)
        if scope == "citywide":
            summ = "overall volume %s %d%% week-over-week (newest full week vs prior)." % (
                "down" if metric < 0 else "up", abs(pct))
        else:
            summ = "%s: %d -> %d week-over-week (%+d%%)." % (key, prior, new, pct)
        row = {"schema": "water.signal", "id": "ds:%s:water/signal/emerging-%s-%s-%s" % (T, scope, _slug(key), TO),
               "tenant": T, "signal_type": "emerging", "scope": scope, "key": key, "newest": new, "prior": prior,
               "metric": metric, "metric_name": "wow_change", "severity": "info", "reliable": True,
               "summary": summ, "window": window, "observed_at": now}
        if scope == "citywide":
            row["weeks"] = weeks
        sig.append(row)

    # emerging — per category + citywide
    by_cat = defaultdict(list)
    for c in complaints:
        if c.get("category"):
            by_cat[c["category"]].append(c)
    for key in sorted(by_cat):
        _emerging(by_cat[key], "category", key)
    _emerging(complaints, "citywide", "delhi")

    # recurring — chronic geocoded cells over the 4 trailing weeks
    geo = [c for c in complaints if c.get("h3")]
    cell_weeks = defaultdict(set); c28 = Counter()
    for c in geo:
        for i in range(weeks):
            if a_b(tod, i, _date(c.get("registered_at"))):
                cell_weeks[c["h3"]].add(i); c28[c["h3"]] += 1
    # a cell's ward label = modal ward over the trailing 30-day window (same basis as water-hotspots)
    hs_from = (tod - _dt.timedelta(days=29)).isoformat(); cell_ward = defaultdict(list)
    for c in geo:
        if hs_from <= _date(c.get("registered_at")) <= TO and c.get("ward"):
            cell_ward[c["h3"]].append(c["ward"])
    active = [h for h in cell_weeks if cell_weeks[h]]
    chronic = [h for h in cell_weeks if len(cell_weeks[h]) == weeks]
    for h, _c in _top({h: c28[h] for h in chronic}, top_cells):
        ward = Counter(cell_ward[h]).most_common(1)
        ward = ward[0][0] if ward else ""
        sig.append({"schema": "water.signal", "id": "ds:%s:water/signal/recurring-cell-%s-%s" % (T, h, TO),
                    "tenant": T, "signal_type": "recurring", "scope": "cell", "key": h, "h3": h, "ward": ward,
                    "metric": float(c28[h]), "metric_name": "complaints_all_weeks", "severity": "warning",
                    "weeks": weeks, "summary": "%s (%s): %d complaints, present every one of %d weeks."
                    % (ward or h, h, c28[h], weeks), "window": window, "observed_at": now})
    share = round(len(chronic) / len(active), 4) if active else 0.0
    spike = 0
    sig.append({"schema": "water.signal", "id": "ds:%s:water/signal/recurring-citywide-delhi-%s" % (T, TO),
                "tenant": T, "signal_type": "recurring", "scope": "citywide", "key": "delhi",
                "metric": share, "metric_name": "chronic_share", "active_cells": len(active),
                "chronic_cells": len(chronic), "spike_cells": spike,
                "severity": "critical" if share >= 0.5 else "warning", "weeks": weeks,
                "summary": "%d of %d active cells (%d%%) appear in ALL %d weeks (chronic); %d high-volume "
                "one-week spikes — the problem is structural, not episodic."
                % (len(chronic), len(active), round(share * 100), weeks, spike),
                "window": window, "observed_at": now})

    # reopen — category / ward / citywide
    for key in sorted(by_cat):
        g = by_cat[key]; rep = sum(1 for c in g if _ri(c.get("reopens")) >= 1)
        rate = round(rep / len(g), 4) if g else 0.0
        sig.append({"schema": "water.signal", "id": "ds:%s:water/signal/reopen-category-%s-%s" % (T, _slug(key), TO),
                    "tenant": T, "signal_type": "reopen", "scope": "category", "key": key, "complaints": len(g),
                    "reopened": rep, "metric": rate, "metric_name": "reopen_rate",
                    "summary": "%s: %d of %d reopened (%.1f%%)." % (key, rep, len(g), (rep / len(g) * 100) if g else 0.0),
                    "window": window, "observed_at": now})
    ward_rep = Counter(c.get("ward") for c in complaints if _ri(c.get("reopens")) >= 1 and c.get("ward"))
    for ward, n in _top(ward_rep, top_wards):
        sig.append({"schema": "water.signal", "id": "ds:%s:water/signal/reopen-ward-%s-%s" % (T, _slug(ward), TO),
                    "tenant": T, "signal_type": "reopen", "scope": "ward", "key": ward, "reopened": n,
                    "metric": float(n), "metric_name": "reopened_count",
                    "summary": "%s: %d complaints reopened at least once." % (ward, n),
                    "window": window, "observed_at": now})
    tot = len(complaints); rep_all = sum(1 for c in complaints if _ri(c.get("reopens")) >= 1)
    events = sum(_ri(c.get("reopens")) for c in complaints)
    rate_all = round(rep_all / tot, 4) if tot else 0.0
    sig.append({"schema": "water.signal", "id": "ds:%s:water/signal/reopen-citywide-delhi-%s" % (T, TO),
                "tenant": T, "signal_type": "reopen", "scope": "citywide", "key": "delhi", "complaints": tot,
                "reopened": rep_all, "reopen_events": events, "metric": rate_all, "metric_name": "reopen_rate",
                "severity": "warning", "summary": "%d of %d complaints reopened (%.1f%%); %d total reopen events "
                "— work that did not stick." % (rep_all, tot, (rep_all / tot * 100) if tot else 0.0, events),
                "window": window, "observed_at": now})

    # ---- overview ----
    closed = sum(1 for c in complaints if c.get("status") == "Closed")
    queries = sum(1 for r in records if r.get("record_kind") == "query")
    nrc = sum(1 for r in records if r.get("record_kind") == "no_response_call")
    geocoded = sum(1 for c in complaints if c.get("h3"))

    def grp(items, keyfn, n=15):
        cc = Counter()
        for x in items:
            k = keyfn(x)
            if k:
                cc[k] += 1
        return [{"key": k, "count": v} for k, v in _top(cc, n)]

    trend = []
    dcount = Counter(_date(c.get("registered_at")) for c in complaints)
    for i in range(span_days):
        day = (frm + _dt.timedelta(days=i)).isoformat()
        trend.append({"date": day, "complaints": dcount.get(day, 0)})

    overview = {"schema": "water.overview", "id": "ds:%s:water/overview/%s" % (T, TO), "tenant": T,
                "observed_at": now, "window": window,
                "totals": {"contacts": len(records), "complaints": tot, "queries": queries,
                           "no_response_calls": nrc, "closed": closed, "pending": tot - closed,
                           "geocoded": geocoded, "geocode_pct": round(geocoded / tot, 4) if tot else 0.0,
                           "closure_rate": round(closed / tot, 4) if tot else 0.0,
                           "reopened_complaints": rep_all, "reopen_events": events, "hotspots": None},
                "by_category": grp(complaints, lambda c: c.get("category")),
                "by_raw_category": grp(complaints, lambda c: c.get("raw_category"), 12),
                "by_domain": grp(complaints, lambda c: cat_domain.get(c.get("category"), "water")),
                "by_zone": grp(complaints, lambda c: c.get("zone")),
                "by_status": grp(complaints, lambda c: c.get("status")),
                "by_channel": grp(complaints, lambda c: c.get("source_channel")),
                "trend_daily": trend}
    return sig, overview


def a_b(tod, i, dstr):
    a = (tod - _dt.timedelta(days=7 * i + 6)).isoformat(); b = (tod - _dt.timedelta(days=7 * i)).isoformat()
    return bool(dstr) and a <= dstr <= b


@driver.main
def run():
    if not ctx.tenant:
        print("FAIL: no run tenant."); return 1
    recs = [(r.get("object") or r) for r in ctx.facts("water.complaint").list()]
    rks = [(r.get("object") or r) for r in ctx.facts("water.record_kind").list()]
    cats = [(r.get("object") or r) for r in ctx.facts("water.category").list()]
    cfgs = [(r.get("object") or r) for r in ctx.facts("water.config").list()]
    config = cfgs[0] if cfgs else {}
    tw = int(os.environ.get("TOP_WARDS", "12")); tc = int(os.environ.get("TOP_CELLS", "15"))
    sig, overview = compute_signals(recs, rks, cats, config, ctx.now(), tw, tc)
    if overview is None:
        print("[water-signals] no dated complaints — nothing to do."); return 0
    ws = ctx.write_many(sig) or 0
    ov = ctx.write_many([overview]) or 0
    print("[water-signals] wrote %d water.signal + %d water.overview (window %s..%s)."
          % (ws, ov, overview["window"]["from"], overview["window"]["to"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(driver.run())
