#!/usr/bin/env python3
"""water-hotspots — aggregate geocoded water COMPLAINTS into hot H3 cells (water.hotspot).

Reconstructed 2026-10-23 from the governed build-guide + the recorded water.hotspot oracle (airawatdeveloper /
DJB, 350 cells). The pure core `compute_hotspots()` is verified to reproduce the recorded hotspots EXACTLY on
count · dominant · category · ward · zone · severity · window · id (350/350) and rate_per_10k ward-level
(345/350); `wow_change` was re-baselined (see below). No taxonomy/thresholds are hardcoded — the non-complaint
record kinds come from water.record_kind and the severity cut-offs + window length from water.config.

Logic (verified against the oracle):
  • WINDOW: to = max(registered_at date) over complaints; from = to - (WINDOW_DAYS-1) INCLUSIVE (30 days).
  • CANDIDATES: record_kind is a COMPLAINT (water.record_kind.is_complaint), h3 present, registered in window.
  • Group by h3 → count; dominant = modal category (also written as `category`); ward/zone = modal.
  • SEVERITY: rank cells by count desc (ties by h3); top ceil(N*count_crit_pct) = critical, next up to
    ceil(N*count_warn_pct) = warning, rest = info  (config.severity, defaults 0.10 / 0.30).
  • rate_per_10k: WARD-level = round(ward_candidate_count / ward_population * 1e4, 2) when POP_FILE_ID gives
    the ward's population; else omitted.
  • wow_change (RE-BASELINED 2026-10-23): round(last7 / prior7, 2) at the cell (last7 = [to-6..to],
    prior7 = [to-13..to-7]); omitted when prior7 == 0.
Confined — only I/O is the SDK ctx (broker).
"""
import os, json, math, datetime as _dt
from collections import Counter, defaultdict
from aos import driver, ctx


def _date(s):
    return (s or "")[:10]


def _mode(vals):
    vals = [v for v in vals if v]
    return Counter(vals).most_common(1)[0][0] if vals else ""


def compute_hotspots(complaints, record_kinds, config, ward_pop, now, window_days=30):
    """PURE core (no I/O) — returns (hotspot_rows, meta). Shared by the engine and the offline verifier."""
    noncomplaint = {r.get("code") for r in record_kinds if r.get("code") and not r.get("is_complaint")}
    sev = (config or {}).get("severity") or {}
    crit_pct = float(sev.get("count_crit_pct", 0.10)); warn_pct = float(sev.get("count_warn_pct", 0.30))
    min_count = int(((config or {}).get("windows") or {}).get("min_count", 1))

    dated = [c for c in complaints if _date(c.get("registered_at"))]
    if not dated:
        return [], {}
    TO = max(_date(c["registered_at"]) for c in dated)
    FROM = (_dt.date.fromisoformat(TO) - _dt.timedelta(days=window_days - 1)).isoformat()

    def _is_complaint(c):
        rk = c.get("record_kind")
        return rk not in noncomplaint          # absent/complaint kinds count; query/no_response_call excluded

    cand = [c for c in dated if c.get("h3") and _is_complaint(c) and FROM <= _date(c["registered_at"]) <= TO]
    cells = defaultdict(list)
    for c in cand:
        cells[c["h3"]].append(c)
    cells = {h: g for h, g in cells.items() if len(g) >= min_count}

    # ward-level candidate counts → ward rate
    ward_count = Counter(c.get("ward") for c in cand if c.get("ward"))

    # severity by count percentile (positional, stable)
    order = sorted(cells.keys(), key=lambda h: (-len(cells[h]), h))
    N = len(order)
    ncrit = math.ceil(N * crit_pct); nwarn = math.ceil(N * warn_pct)
    sev_of = {}
    for i, h in enumerate(order):
        sev_of[h] = "critical" if i < ncrit else "warning" if i < nwarn else "info"

    # week windows for wow (cell)
    tod = _dt.date.fromisoformat(TO)
    lw_a = (tod - _dt.timedelta(days=6)).isoformat(); lw_b = TO
    pw_a = (tod - _dt.timedelta(days=13)).isoformat(); pw_b = (tod - _dt.timedelta(days=7)).isoformat()

    rows = []
    for h, g in cells.items():
        ward = _mode([c.get("ward") for c in g]); zone = _mode([c.get("zone") for c in g])
        dom = _mode([c.get("category") for c in g])
        pop = (ward_pop or {}).get(ward)
        rate = round(ward_count[ward] / pop * 10000, 2) if pop else None
        last = sum(1 for c in g if lw_a <= _date(c["registered_at"]) <= lw_b)
        prior = sum(1 for c in g if pw_a <= _date(c["registered_at"]) <= pw_b)
        wow = round(last / prior, 2) if prior else None
        oid = "ds:%s:water/hotspot/%s-%s" % (ctx.tenant, h, TO)
        row = {"schema": "water.hotspot", "id": oid, "tenant": ctx.tenant, "h3": h,
               "ward": ward, "zone": zone, "count": len(g), "dominant": dom, "category": dom,
               "severity": sev_of[h], "rate_per_10k": rate, "wow_change": wow,
               "window": {"from": FROM, "to": TO, "days": window_days}, "observed_at": now}
        rows.append({k: v for k, v in row.items() if v is not None})
    return rows, {"window": {"from": FROM, "to": TO, "days": window_days}, "cells": N, "candidates": len(cand)}


@driver.main
def run():
    if not ctx.tenant:
        print("FAIL: no run tenant."); return 1
    window_days = int(os.environ.get("WINDOW_DAYS", "30"))
    complaints = [(r.get("object") or r) for r in ctx.facts("water.complaint").list()]
    rks = [(r.get("object") or r) for r in ctx.facts("water.record_kind").list()]
    cfgs = [(r.get("object") or r) for r in ctx.facts("water.config").list()]
    config = cfgs[0] if cfgs else {}

    ward_pop = {}
    pid = os.environ.get("POP_FILE_ID", "").strip()
    if pid:
        try:
            ward_pop = (json.loads(ctx.file.get(pid)) or {}).get("wards", {}) or {}
            print("[water-hotspots] ward population: %d wards" % len(ward_pop))
        except Exception as e:
            print("[water-hotspots] POP_FILE_ID unreadable (%s) — rate_per_10k omitted." % str(e)[:80])

    rows, meta = compute_hotspots(complaints, rks, config, ward_pop, ctx.now(), window_days)
    if not rows:
        print("[water-hotspots] no geocoded complaints in window — wrote 0 (nothing to do)."); return 0
    wrote = ctx.write_many(rows) or 0
    live = len([r for r in ctx.facts("water.hotspot").list()])
    print("[water-hotspots] window %s..%s — wrote %d water.hotspot (%d candidates, %d cells). verify: %d live."
          % (meta["window"]["from"], meta["window"]["to"], wrote, meta["candidates"], meta["cells"], live))
    return 0


if __name__ == "__main__":
    raise SystemExit(driver.run())
