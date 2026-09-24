#!/usr/bin/env python3
"""Tax Gap Engine — the config-driven analytical spine for property-tax enforcement.

It triangulates INDEPENDENT signals against the cadastral master to find unassessed / under-assessed parcels:
  • fuzzy RECORD LINKAGE across datasets (mobile / name / address, weighted) — which utilities/ledger rows are the same
    property, and which utilities have NO tax assessment;
  • SPATIAL JOIN (point-in-polygon) of geocoded ledger + utility points onto geo.parcel, with GEOMETRIC-HYGIENE flags
    (inside one parcel / on a boundary / spanning multiple / outside all);
  • GEOCODE banding (high/medium/low) from each record's geocode_confidence;
  • CHANGE detection (tax.change_signal, from satellite/AlphaEarth) as corroborating evidence.
Mismatches become scored `tax.candidate`s (+ a governance.recommendation — AI flags, humans decide). EVERY threshold and
fuzzy weight comes from `tax.match_policy` — nothing is hardcoded. Handlers: matchpolicy-get · matchpolicy-set ·
match-preview · gap-scan. Writes as component://tax-gap-engine obo the user.
"""
import time as _t, re as _re, os as _os, json as _json

_ADMIN = {"platform.tenant-admin", "platform.steward", "admin"}

# The DEFAULT policy — returned until a tenant edits it via the Tax Config Console. Every knob is overridable.
DEFAULT_POLICY = {
    "geocode": {"floor": 0.5, "bands": {"high": 0.85, "medium": 0.65, "low": 0.4}},
    "address_parse": {"high": {"min_tokens": 5}, "medium": {"min_tokens": 3}, "low": {"min_tokens": 1}},
    "geometry_hygiene": {"on_boundary_tolerance_m": 5, "single_parcel": "accept", "on_boundary": "flag",
                         "multi_parcel": "flag", "outside_all": "flag"},
    "fuzzy_weights": {"mobile": 0.4, "name": 0.3, "address": 0.3},
    "fuzzy_thresholds": {"match": 0.85, "review": 0.6},
    # LINKAGE LADDER: try an EXACT key-join first (a clean survey↔register key like surveyid↔uid), then fall back
    # to the fuzzy/spatial machinery. A city WITHOUT a clean key just leaves key_join off and uses the fallback.
    "link": {"key_join": {"enabled": True, "parcel_field": "parcel_id", "assessment_field": "uid"}},
    # parcel_is_footprint: when the cadastre IS the built-property survey (each geo.parcel is a surveyed building),
    # a parcel with no assessment is itself the "footprint with no tax record" — no separate geo.building needed.
    "candidate": {"min_confidence": 0.5, "parcel_is_footprint": False,
                  "signal_weights": {"utility_no_tax": 0.4, "footprint_no_assessment": 0.3,
                                     "change_detected": 0.2, "under_assessed": 0.1}},
}


def _now():
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())


def _deep(base, over):
    """Overlay `over` on `base` recursively (so a partial saved policy still gets every default filled)."""
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _deep(base.get(k, {}), v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


# --- fuzzy record linkage (pure; no deps) --------------------------------------------------------------------
def _norm(s):
    return _re.sub(r"\s+", " ", _re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())).strip()


def _digits(s):
    return _re.sub(r"\D", "", str(s or ""))[-10:]


def _tokens(s):
    return set(t for t in _norm(s).split() if t)


def _token_sim(a, b):                                        # Dice coefficient over token sets — robust to word order
    A, B = _tokens(a), _tokens(b)
    if not A or not B:
        return 0.0
    return round(2 * len(A & B) / (len(A) + len(B)), 4)


def _mobile_sim(a, b):
    da, db = _digits(a), _digits(b)
    return 1.0 if (da and da == db) else 0.0


def match_score(a, b, weights):
    """Weighted record-linkage score ∈ [0,1] between two records, using the policy's field weights. A field with no
    data on either side is dropped and the weights renormalised, so a missing mobile doesn't sink an otherwise good match."""
    parts = [("mobile", _mobile_sim(a.get("mobile"), b.get("mobile")), a.get("mobile") and b.get("mobile")),
             ("name", _token_sim(a.get("owner") or a.get("consumer_name"), b.get("owner") or b.get("consumer_name")),
              (a.get("owner") or a.get("consumer_name")) and (b.get("owner") or b.get("consumer_name"))),
             ("address", _token_sim(a.get("address_string"), b.get("address_string")),
              a.get("address_string") and b.get("address_string"))]
    num = den = 0.0; contrib = {}
    for field, sim, present in parts:
        w = (weights or {}).get(field, 0)
        if present and w:
            num += w * sim; den += w; contrib[field] = round(sim, 3)
    return (round(num / den, 4) if den else 0.0), contrib


# --- geometry (pure point-in-polygon + hygiene) --------------------------------------------------------------
def _pt(p):
    if not p:
        return None
    if isinstance(p, dict):
        lon, lat = p.get("lon", p.get("lng")), p.get("lat")
        return (lon, lat) if lon is not None and lat is not None else None
    if isinstance(p, (list, tuple)) and len(p) >= 2:
        return (p[0], p[1])
    return None


def _rings(geom):
    """Extract coordinate rings from a GeoJSON Polygon/MultiPolygon (best-effort)."""
    if not isinstance(geom, dict):
        return []
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "Polygon" and c:
        return [c[0]]
    if t == "MultiPolygon" and c:
        return [poly[0] for poly in c if poly]
    return []


def _in_ring(pt, ring):
    x, y = pt; inside = False; n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]; x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1):
            inside = not inside
    return inside


def _in_bbox(pt, bbox):
    if not bbox or len(bbox) < 4:
        return False
    return bbox[0] <= pt[0] <= bbox[2] and bbox[1] <= pt[1] <= bbox[3]


def _near_edge_m(pt, ring, tol_m):
    """True if pt is within ~tol_m metres of any edge of the ring (deg→m ≈ 111_320 at the equator; good enough for hygiene)."""
    tol = (tol_m or 0) / 111320.0
    x, y = pt
    for i in range(len(ring)):
        x1, y1 = ring[i][0], ring[i][1]; x2, y2 = ring[(i + 1) % len(ring)][0], ring[(i + 1) % len(ring)][1]
        dx, dy = x2 - x1, y2 - y1; L2 = dx * dx + dy * dy
        t = 0 if L2 == 0 else max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / L2))
        d = ((x - (x1 + t * dx)) ** 2 + (y - (y1 + t * dy)) ** 2) ** 0.5
        if d <= tol:
            return True
    return False


def locate(pt, parcels, tol_m):
    """Point → (parcel_id, hygiene) against the parcel set. hygiene ∈ single_parcel|on_boundary|multi_parcel|outside_all."""
    p = _pt(pt)
    if not p:
        return None, "no_point"
    hits, on_edge = [], False
    for pc in parcels:
        rings = _rings(pc.get("geometry"))
        contained = any(_in_ring(p, r) for r in rings) if rings else _in_bbox(p, pc.get("bbox"))
        if contained:
            hits.append(pc.get("parcel_id"))
            if rings and any(_near_edge_m(p, r, tol_m) for r in rings):
                on_edge = True
    if not hits:
        return None, "outside_all"
    if len(hits) > 1:
        return hits[0], "multi_parcel"
    return hits[0], ("on_boundary" if on_edge else "single_parcel")


def _centroid(geom):
    """A representative interior point of a polygon (mean of its outer ring) — used to spatially place a footprint."""
    rings = _rings(geom)
    if not rings or not rings[0]:
        return None
    pts = rings[0]
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def geocode_band(conf, bands):
    if conf is None:
        return "unknown"
    if conf >= bands.get("high", 0.85):
        return "high"
    if conf >= bands.get("medium", 0.65):
        return "medium"
    if conf >= bands.get("low", 0.4):
        return "low"
    return "reject"


# --- handlers ------------------------------------------------------------------------------------------------
def _policy(ctx):
    rows = ctx["kfind"]("tax.match_policy")
    saved = next((r for r in rows if r.get("status") != "draft"), (rows[0] if rows else None))
    return _deep(DEFAULT_POLICY, saved or {}), saved


def h_matchpolicy_get(ctx):
    pol, saved = _policy(ctx)
    return {"policy": pol, "saved": bool(saved), "id": (saved or {}).get("id")}, None


def h_matchpolicy_set(ctx):
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    if not (set(ctx["claims"].get("roles", [])) & _ADMIN):
        return None, "editing the matching policy requires a tenant admin"
    pid = f"ds:{tenant}:tax/match_policy/default"
    prior = ctx["kget"](pid) or {}
    merged = _deep(_deep(DEFAULT_POLICY, prior), body.get("policy") or {})
    rec = {"schema": "tax.match_policy", "id": pid, "tenant": tenant, "name": body.get("name") or "Tax matching policy",
           "status": "active", "updated_by": sub, "updated_at": _now(), "created_at": prior.get("created_at") or _now()}
    for k in ("geocode", "address_parse", "geometry_hygiene", "fuzzy_weights", "fuzzy_thresholds", "candidate"):
        rec[k] = merged.get(k)
    ctx["kwrite"](pid, rec)
    return {"saved": pid, "policy": merged}, None


def h_match_preview(ctx):
    """Tune weights: given two records + (optional) weights, return the score breakdown. Used by the config console."""
    body = ctx["body"]; pol, _ = _policy(ctx)
    weights = body.get("weights") or pol["fuzzy_weights"]
    score, contrib = match_score(body.get("a") or {}, body.get("b") or {}, weights)
    th = pol["fuzzy_thresholds"]
    verdict = "match" if score >= th.get("match", 0.85) else ("review" if score >= th.get("review", 0.6) else "no_link")
    return {"score": score, "contributions": contrib, "verdict": verdict, "weights": weights, "thresholds": th}, None


def h_gap_scan(ctx):
    """THE SPINE. Spatially join + fuzzy-link the layers against the cadastre, then score tax-gap candidates. Reads real
    records only (no fabrication) — with no source data it simply finds nothing. Returns a summary + writes candidates."""
    tenant = ctx["tenant"]; pol, _ = _policy(ctx)
    tol = pol["geometry_hygiene"].get("on_boundary_tolerance_m", 5)
    review_th = pol["fuzzy_thresholds"].get("review", 0.6)
    sw = pol["candidate"]["signal_weights"]; min_conf = pol["candidate"].get("min_confidence", 0.5)
    kj = (pol.get("link") or {}).get("key_join") or {}
    kj_on = bool(kj.get("enabled")); kj_pf = kj.get("parcel_field", "parcel_id"); kj_af = kj.get("assessment_field", "uid")
    parcel_is_footprint = bool(pol["candidate"].get("parcel_is_footprint"))
    parcels = ctx["kfind"]("geo.parcel")
    assessments = ctx["kfind"]("tax.assessment")
    utilities = ctx["kfind"]("util.connection")
    footprints = ctx["kfind"]("geo.building")
    changes = ctx["kfind"]("tax.change_signal")
    now = _now()

    # 1) spatial-join + geocode-band each located record onto a parcel; index assessments by parcel
    def join(recs):
        for r in recs:
            pid, hyg = locate(r.get("point"), parcels, tol)
            r["_parcel"] = pid or r.get("parcel_id"); r["_hygiene"] = hyg
            r["_band"] = geocode_band(r.get("geocode_confidence"), pol["geocode"]["bands"])
    join(assessments); join(utilities)
    assess_by_parcel = {}
    for a in assessments:
        if a.get("_parcel"):
            assess_by_parcel.setdefault(a["_parcel"], []).append(a)
    # LINKAGE TIER 1 — exact key-join: index assessments by their survey key (uid). A parcel whose parcel_id equals
    # an assessment's uid is REGISTERED by that clean key alone; the spatial/fuzzy tiers below are the fallback for
    # rows that lack such a key. Cities without a clean key just leave key_join off in the policy.
    assess_by_key = {}
    if kj_on:
        for a in assessments:
            k = (a.get(kj_af) or "").strip() if isinstance(a.get(kj_af), str) else a.get(kj_af)
            if k:
                assess_by_key.setdefault(k, []).append(a)

    # 2) per-parcel signal fusion
    scanned = 0; cands = []; by_signal = {}
    def bump(k): by_signal[k] = by_signal.get(k, 0) + 1
    # footprints don't carry a parcel_id — SPATIALLY place each (its centroid) onto a parcel
    fp_parcels = set()
    for f in footprints:
        c = _centroid(f.get("geometry"))
        if c:
            pidf, _hyg = locate({"lon": c[0], "lat": c[1]}, parcels, tol)
            if pidf:
                fp_parcels.add(pidf)
    for pc in parcels:
        scanned += 1; pid = pc.get("parcel_id"); ev = []
        # TIER 1 exact key-join, then TIER 2 the spatially-joined index: registered if EITHER links an assessment.
        key_hit = assess_by_key.get(pc.get(kj_pf)) if kj_on else None
        has_assess = bool(assess_by_parcel.get(pid)) or bool(key_hit)
        # utility_no_tax: a utility on this parcel whose fuzzy-link to any assessment is below the review threshold.
        # Compare against assessments linked by EITHER the spatial index OR the exact key-join, so a registered parcel's
        # utility (which fuzzy-matches its key-joined assessment) is correctly suppressed.
        linked_assess = assess_by_parcel.get(pid, []) + (list(key_hit) if key_hit else [])
        util_here = [u for u in utilities if u.get("_parcel") == pid]
        matched_util = None
        for u in util_here:
            best = max((match_score(u, a, pol["fuzzy_weights"])[0] for a in linked_assess), default=0.0)
            if best < review_th:
                ev.append({"signal": "utility_no_tax", "detail": f"{u.get('utility_type')} conn {u.get('account_no') or u.get('uid')} (best link {best})", "weight": sw.get("utility_no_tax", 0)})
                matched_util = u.get("id"); bump("utility_no_tax"); break
        # footprint_no_assessment (UNREGISTERED): a built property with no tax record found by key OR spatial OR fuzzy.
        # The footprint is either a separately-placed geo.building OR — when the cadastre IS the built-property survey
        # (parcel_is_footprint) — the surveyed parcel itself. This is the core "in the survey, not in the register" gap.
        if not has_assess and (pid in fp_parcels or parcel_is_footprint):
            detail = "in the property survey, no matching tax record (key/spatial/fuzzy)" if parcel_is_footprint \
                else "building footprint present, no assessment"
            ev.append({"signal": "footprint_no_assessment", "detail": detail, "weight": sw.get("footprint_no_assessment", 0)})
            bump("footprint_no_assessment")
        # change_detected: a new/expanded change signal on this parcel
        ch = next((c for c in changes if c.get("parcel_id") == pid and c.get("change_type") in ("new_build", "expanded")), None)
        if ch:
            ev.append({"signal": "change_detected", "detail": f"{ch.get('change_type')} (score {ch.get('score')})", "weight": sw.get("change_detected", 0)})
            bump("change_detected")
        if not ev:
            continue
        conf = round(min(1.0, sum(e["weight"] for e in ev)), 4)
        if conf < min_conf:
            continue
        cid = f"ds:{tenant}:tax/candidate/{pid.rsplit('/', 1)[-1]}"
        est = None
        a0 = (assess_by_parcel.get(pid) or [{}])[0]
        # carry the parcel's location onto the candidate so it RENDERS on the map (a Point coord, or a polygon centroid)
        _g = pc.get("geometry"); _pt = None
        if isinstance(_g, dict):
            if _g.get("type") == "Point" and _g.get("coordinates"):
                _c = _g["coordinates"]; _pt = {"lon": _c[0], "lat": _c[1]}
            else:
                _c = _centroid(_g); _pt = {"lon": _c[0], "lat": _c[1]} if _c else None
        cand = {"schema": "tax.candidate", "id": cid, "tenant": tenant, "parcel_id": pid, "point": _pt,
                "ward": pc.get("ward"), "evidence": ev, "confidence": conf, "estimated_revenue": est,
                "recommended_action": "field-verify", "matched_utility": matched_util,
                "matched_assessment": a0.get("id"), "status": "flagged", "created_at": now}
        ctx["kwrite"](cid, cand)
        # ADR-0029: an AI flag is a RECOMMENDATION, never a decision
        rid = f"ds:{tenant}:governance/recommendation/taxgap-{pid.rsplit('/', 1)[-1]}"
        ctx["kwrite"](rid, {"schema": "governance.recommendation", "id": rid, "tenant": tenant,
            "title": f"Tax-gap candidate — {pid.rsplit('/', 1)[-1]}", "summary": "field-verify a suspected tax gap",
            "proposed_action": "field-verify", "rationale": "; ".join(e["signal"] for e in ev),
            "evidence": [pid] + [e for e in [matched_util] if e], "confidence": conf, "method": "rule",
            "produced_by": "component://ds:airawatdeveloper:engine/tax-gap-engine@0.1.0", "score": conf, "created_at": now})
        cand["recommendation"] = rid; ctx["kwrite"](cid, cand)
        cands.append({"candidate": cid, "parcel": pid, "confidence": conf, "signals": [e["signal"] for e in ev]})
    cands.sort(key=lambda c: -c["confidence"])
    return {"scanned_parcels": scanned, "candidates": len(cands), "by_signal": by_signal,
            "top": cands[:20], "policy_used": {"min_confidence": min_conf, "review_threshold": review_th}}, None


HANDLERS = {"matchpolicy-get": h_matchpolicy_get, "matchpolicy-set": h_matchpolicy_set,
            "match-preview": h_match_preview, "gap-scan": h_gap_scan}


# ── headless run (scheduler /run) ────────────────────────────────────────────────────────────────────────
# When the scheduler runs this engine as a component (RUN_TOKEN present), it executes gap-scan headless against
# the confined broker — same read (/v1/query, /v1/read) + write (/v1/write/batch) contract the drivers use — so a
# periodic tax-gap re-scan is just a scheduled run. The in-process verb dispatch (HANDLERS) is unchanged.
def _bcall(path, body):
    import urllib.request
    api = _os.environ.get("AIRAWAT_API", "http://localhost:8080")
    req = urllib.request.Request(api + path, data=_json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": _os.environ.get("RUN_TOKEN", "")}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return _json.loads(r.read() or b"{}")


def _bquery(scope):
    return [r.get("object") or r for r in (_bcall("/v1/query", {"scope": scope, "limit": 200000}).get("results") or [])]


def _bwrite(oid, obj):
    _bcall("/v1/write/batch", {"items": [{"scope": obj.get("schema"), "id": oid, "object": obj}], "asserted_at": _now()})


def _run_tenant():
    try:
        t = (_bcall("/v1/context", {}) or {}).get("tenant")
        if t:
            return t
    except Exception:
        pass
    return _os.environ.get("TENANT") or _os.environ.get("WRITE_TENANT") or ""


def main():
    action = _os.environ.get("RUN_HANDLER") or ("gap-scan" if _os.environ.get("RUN_TOKEN") else "")
    if action != "gap-scan":
        print("Tax Gap Engine — handlers:", ", ".join(sorted(HANDLERS))); return 0
    tenant = _run_tenant()
    if not tenant:
        print("[tax-gap-engine] FAIL: could not resolve the run tenant"); return 1
    ctx = {"tenant": tenant, "kfind": _bquery, "kget": lambda i: None, "kwrite": _bwrite,
           "body": {}, "sub": "component://tax-gap-engine", "claims": {"roles": []}}
    res, err = h_gap_scan(ctx)
    if err:
        print("[tax-gap-engine] gap-scan error:", err); return 1
    print(f"[tax-gap-engine] gap-scan tenant={tenant}: scanned={res['scanned_parcels']} "
          f"candidates={res['candidates']} by_signal={res['by_signal']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
