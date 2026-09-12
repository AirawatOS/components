"""Funding Engine — the information-gates-money loop for a funding agency (ADR-0072 funder side).

publish-program → award (stacking shares) → stack check (Σ awards ≥ ask) → GATED tranche release (only when a
verified milestone exists). Release posts a revenue finance.transaction into the project ledger via finance-engine.
The rule the whole design turns on: money cannot move until the information (a verified milestone) moves.
"""
import time as _t

CID = "ds:airawatdeveloper:engine/funding-engine"
FIN = "ds:airawatdeveloper:engine/finance-engine"
def _now(): return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
def _slug(s): return str(s).split("/")[-1].replace(":", "-")

def _proposal(ctx, pid): return ctx["kget"](pid)
def _ask(prop): return float(((prop or {}).get("ask") or {}).get("amount_cr") or 0)

def h_publish_program(ctx):
    tenant, sub, b = ctx["tenant"], ctx.get("sub"), ctx.get("body") or {}
    funder = b.get("funder"); title = b.get("title"); env = b.get("envelope_cr")
    if not funder or not title or env is None:
        return {"error": "funder + title + envelope_cr required"}, None
    scheme = b.get("scheme") or title
    pid = f"ds:{tenant}:funding/program/{_slug(b.get('program_id') or scheme)}"
    rec = {"schema": "funding.program", "id": pid, "tenant": tenant, "funder": funder, "title": title,
           "description": b.get("description"), "scheme": scheme, "envelope_cr": float(env),
           "currency": b.get("currency", "INR"), "co_funders": b.get("co_funders"),
           "eligibility": b.get("eligibility"), "reporting": b.get("reporting") or {"milestone_gated": True},
           "status": "open", "created_at": _now(),
           "prov": {"method": "funder programme publication", "actor": sub}}
    ctx["kwrite"](pid, rec)
    return {"program": pid, "funder": funder, "envelope_cr": env, "co_funders": b.get("co_funders")}, None

def h_award(ctx):
    tenant, sub, b = ctx["tenant"], ctx.get("sub"), ctx.get("body") or {}
    program = b.get("program"); proposal = b.get("proposal"); funder = b.get("funder"); amt = b.get("amount_cr")
    if not program or not proposal or not funder or amt is None:
        return {"error": "program + proposal + funder + amount_cr required"}, None
    prop = _proposal(ctx, proposal)
    project = b.get("project") or (prop or {}).get("project")
    aid = f"ds:{tenant}:funding/award/{_slug(proposal)}-{_slug(funder)}"
    rec = {"schema": "funding.award", "id": aid, "tenant": tenant, "program": program, "proposal": proposal,
           "project": project, "funder": funder, "share": b.get("share"), "amount_cr": float(amt),
           "currency": b.get("currency", "INR"), "status": "committed", "released_cr": 0.0,
           "decision": b.get("decision"), "awarded_at": _now(),
           "prov": {"method": "funder sanction → signed commitment", "actor": sub}}
    ctx["kwrite"](aid, rec)
    stk = _stack(ctx, proposal, prop); stk.pop("_awards", None)
    return {"award": aid, "funder": funder, "amount_cr": amt, **stk}, None

def _tenant_of(oid, default):
    return oid.split(":")[1] if isinstance(oid, str) and oid.startswith("ds:") and len(oid.split(":")) > 2 else default

def _read_proposal_x(ctx, proposal):
    """Read the proposal — own tenant, else cross-tenant (the funder reads the implementer's proposal)."""
    prop = ctx["kget"](proposal)
    if not prop and ctx.get("kfind_x"):
        rows = ctx["kfind_x"]("project.proposal", _tenant_of(proposal, ctx["tenant"]), {"id": proposal})
        prop = rows[0] if rows else None
    return prop

def _stack(ctx, proposal, prop=None, funders=None):
    prop = prop or _read_proposal_x(ctx, proposal)
    ask = _ask(prop)
    awards = list(ctx["kfind"]("funding.award", {"proposal": proposal}))     # own-tenant awards
    seen = {a["id"] for a in awards}
    kx = ctx.get("kfind_x")
    for tn in (funders or []):                                               # + each funder tenant's awards (aggregator lane)
        if not kx or tn == ctx["tenant"]:
            continue
        for a in kx("funding.award", tn, {"proposal": proposal}):
            if a["id"] not in seen:
                awards.append(a); seen.add(a["id"])
    committed = round(sum(float(a.get("amount_cr") or 0) for a in awards), 2)
    return {"ask_cr": ask, "committed_cr": committed, "gap_cr": round(max(0.0, ask - committed), 2),
            "funded": committed >= ask > 0, "awards": len(awards), "_awards": awards}

def h_stack(ctx):
    b = ctx.get("body") or {}; proposal = b.get("proposal")
    if not proposal: return {"error": "proposal required"}, None
    s = _stack(ctx, proposal, None, b.get("funders"))
    awards = s.pop("_awards")
    return {"proposal": proposal, **s,
            "breakdown": [{"funder": a.get("funder"), "tenant": _tenant_of(a.get("id"), ""), "amount_cr": a.get("amount_cr"),
                           "share": a.get("share"), "status": a.get("status"), "released_cr": a.get("released_cr")}
                          for a in awards]}, None

def h_release_tranche(ctx):
    tenant, sub, b = ctx["tenant"], ctx.get("sub"), ctx.get("body") or {}
    award_id = b.get("award"); amt = b.get("amount_cr")
    if not award_id or amt is None:
        return {"error": "award + amount_cr required"}, None
    award = ctx["kget"](award_id)
    if not award: return {"error": f"award '{award_id}' not found"}, None
    project = award.get("project")

    # THE GATE — information gates money. Two governed checks (ADR-0072 S3c/d):
    #   (1) a STANDING progress subscription authorises this funder to read the project (revocable, not the blanket grant)
    #   (2) a MILESTONE VC (ADR-0049) verifies against the implementer's published key — a signed proof, not a raw read
    ptenant = _tenant_of(project, ctx["tenant"]); kx = ctx.get("kfind_x")
    funder_tenant = _tenant_of(award.get("funder"), None) or _tenant_of(award_id, ctx["tenant"])
    sub = _active_subscription(ctx, funder_tenant, project, ptenant)
    if not sub:
        return {"error": "no active progress subscription for this funder+project (revoked or never granted)",
                "gate": "BLOCKED", "check": "subscription"}, None
    verified, basis = _verify_milestone_vc(ctx, project)
    if not verified:                                          # fallback: the outcome proxy (back-compat) still counts
        oc = (kx("project.outcome", ptenant, {"project": project}) if (ptenant != ctx["tenant"] and kx)
              else ctx["kfind"]("project.outcome", {"project": project}))
        verified = bool(oc); basis = (oc[0]["id"] + " (outcome proxy)") if oc else None
    if not verified:
        return {"error": "information-gates-money: no verified milestone for this release", "gate": "BLOCKED",
                "hint": "record + verify a milestone (project.outcome / milestone_ref) before releasing"}, None

    # release ≤ remaining commitment
    remaining = float(award.get("amount_cr") or 0) - float(award.get("released_cr") or 0)
    amt = min(float(amt), remaining)
    if amt <= 0:
        return {"error": "nothing left to release on this award", "released_cr": award.get("released_cr")}, None

    # the funder posts its DISBURSEMENT (grant expenditure) in ITS OWN ledger (money leaving the funder tenant)
    exp = b.get("expense_account") or next((a["id"] for a in ctx["kfind"]("finance.account") if a.get("type") == "expense"), None)
    res, err = ctx["call"](FIN, "finance-post", {
        "direction": "expenditure", "amount": amt, "account": exp, "contra_type": "liability",
        "project": project, "memo": f"funding tranche: {award.get('funder')} → {_slug(award.get('proposal'))}",
        "source": award_id, "source_kind": "funding.award", "status": "posted"})
    if err:
        return {"error": f"finance posting failed: {err}", "gate": "OPEN"}, None

    award["released_cr"] = round(float(award.get("released_cr") or 0) + amt, 2)
    award["status"] = "released" if award["released_cr"] >= float(award.get("amount_cr") or 0) - 1e-6 else "partially-released"
    ctx["kwrite"](award_id, award)
    return {"released": amt, "award": award_id, "funder": award.get("funder"), "gate": "OPEN",
            "verified_by": basis, "transaction": res.get("posted"), "award_released_cr": award["released_cr"],
            "award_status": award["status"]}, None

def _read_award_x(ctx, award_id):
    at = _tenant_of(award_id, ctx["tenant"])
    if at == ctx["tenant"]:
        return ctx["kget"](award_id)
    kx = ctx.get("kfind_x")
    rows = kx("funding.award", at, {"id": award_id}) if kx else []
    return rows[0] if rows else None

def h_record_receipt(ctx):
    """Run by the IMPLEMENTER: book the funder's released tranche as REVENUE in its OWN ledger, reconciled against
    the funder's signed award (read cross-tenant). Two sets of books, one shared award — closes the money loop."""
    tenant, sub, b = ctx["tenant"], ctx.get("sub"), ctx.get("body") or {}
    award_id = b.get("award")
    if not award_id: return {"error": "award required"}, None
    award = _read_award_x(ctx, award_id)
    if not award: return {"error": f"award '{award_id}' not found"}, None
    released = float(award.get("released_cr") or 0); project = award.get("project")
    prior = round(sum(float(t.get("amount") or 0) for t in ctx["kfind"]("finance.transaction")
                      if t.get("source") == award_id and t.get("direction") == "revenue"), 2)
    newly = round(released - prior, 2)
    if newly <= 0:
        return {"received": 0.0, "total_received_cr": prior, "note": "ledger already up to date with the award"}, None
    rev = b.get("revenue_account") or next((a["id"] for a in ctx["kfind"]("finance.account") if a.get("type") in ("revenue", "income")), None)
    res, err = ctx["call"](FIN, "finance-post", {
        "direction": "revenue", "amount": newly, "account": rev, "contra_type": "asset",
        "project": project, "memo": f"funding received: {award.get('funder')} tranche", "source": award_id,
        "source_kind": "funding.award", "status": "posted"})
    if err:
        return {"error": f"receipt posting failed: {err}"}, None
    return {"received": newly, "total_received_cr": released, "funder": award.get("funder"),
            "transaction": res.get("posted"), "project": project}, None

def h_received(ctx):
    b = ctx.get("body") or {}; project = b.get("project")
    if not project: return {"error": "project required"}, None
    rev = [t for t in ctx["kfind"]("finance.transaction") if t.get("project") == project and t.get("direction") == "revenue"]
    total = round(sum(float(t.get("amount") or 0) for t in rev), 2)
    return {"project": project, "received_cr": total, "receipts": len(rev),
            "by_source": [{"award": t.get("source"), "amount_cr": t.get("amount"), "at": t.get("date")} for t in rev]}, None

# ---------------------------------------------------------------- S3c: progress subscription (standing, revocable)
def _active_subscription(ctx, funder_tenant, project, ptenant=None):
    ptenant = ptenant or _tenant_of(project, ctx["tenant"]); kx = ctx.get("kfind_x")
    grants = (kx("funding.progress_grant", ptenant, {"project": project}) if (ptenant != ctx["tenant"] and kx)
              else ctx["kfind"]("funding.progress_grant", {"project": project}))
    for g in grants:
        if g.get("status") == "active" and (_tenant_of(g.get("subscriber"), "") == funder_tenant
                                            or g.get("subscriber") == funder_tenant):
            return g
    return None

def h_subscribe(ctx):
    """Run by the IMPLEMENTER: grant a funder a standing, revocable read of one project's progress."""
    tenant, sub, b = ctx["tenant"], ctx.get("sub"), ctx.get("body") or {}
    subscriber = b.get("subscriber"); project = b.get("project")
    if not subscriber or not project: return {"error": "subscriber + project required"}, None
    gid = f"ds:{tenant}:funding/progress_grant/{_slug(project)}-{_slug(subscriber)}"
    ctx["kwrite"](gid, {"schema": "funding.progress_grant", "id": gid, "tenant": tenant, "subscriber": subscriber,
        "project": project, "provider": tenant, "status": "active", "issued_by": sub, "issued_at": _now(),
        "scope": b.get("scope") or ["project.outcome", "project.output", "finance.transaction", "credential.milestone"]})
    return {"subscription": gid, "subscriber": subscriber, "project": project, "status": "active"}, None

def h_revoke_subscription(ctx):
    b = ctx.get("body") or {}; gid = b.get("subscription"); g = ctx["kget"](gid) if gid else None
    if not g: return {"error": "subscription not found"}, None
    g["status"] = "revoked"; g["revoked_at"] = _now(); ctx["kwrite"](gid, g)
    return {"revoked": gid, "status": "revoked"}, None

# ---------------------------------------------------------------- S3d: milestone as a real ADR-0049 VC
def _milestone_vcs(ctx, project):
    """All credential.milestone VCs the implementer (project's tenant) issued for this project — holder-agnostic,
    so the funder verifies by the ISSUER's key regardless of which officer holds the credential."""
    tok = ctx.get("token"); net = ctx.get("network_url")
    if not net: return []
    try:
        import urllib.parse as _up
        lst = ctx["http"]("GET", net + "/v1/credentials?" + _up.urlencode({"issuer_tenant": _tenant_of(project, ctx["tenant"]), "type": "credential.milestone"}), None, tok)
    except Exception:
        return []
    return [vc for vc in (lst.get("credentials") or []) if (vc.get("claims") or {}).get("project") == project]

def _verify_milestone_vc(ctx, project):
    net = ctx.get("network_url"); tok = ctx.get("token")
    for vc in _milestone_vcs(ctx, project):
        try:
            if ctx["http"]("POST", net + "/v1/credentials/verify", {"credential": vc}, tok).get("verified"):
                return True, vc["id"] + " (VC ✓)"
        except Exception:
            pass
    return False, None

def h_milestone(ctx):
    """Run by the IMPLEMENTER: issue a signed credential.milestone VC to each holder funder (ADR-0049)."""
    tenant, b = ctx["tenant"], ctx.get("body") or {}
    project = b.get("project"); milestone = b.get("milestone") or "m1"; holders = b.get("holders") or []
    tok = ctx.get("token"); net = ctx.get("network_url")
    oc = [o for o in ctx["kfind"]("project.outcome", {"project": project})]
    claims = {"project": project, "milestone": milestone, "outcome_ref": (oc[0]["id"] if oc else None),
              "persons_moved": (oc[0].get("persons_moved") if oc else None)}
    issued = []
    for h in holders:
        try:
            r = ctx["http"]("POST", net + "/v1/credentials/issue", {"issuer_tenant": tenant, "holder": h,
                "type": "credential.milestone", "title": f"Milestone {milestone} verified", "claims": claims,
                "subject_ref": project}, tok)
            issued.append({"holder": h, "vc": r.get("issued")})
        except Exception as e:
            issued.append({"holder": h, "error": str(e)[:120]})
    return {"project": project, "milestone": milestone, "issued": issued}, None

# ---------------------------------------------------------------- flow ②: need aggregation up the ladder
def h_need_publish(ctx):
    """Run by a CITY: publish its aggregate welfare need (persons below the floor) — only the aggregate, raw stays local."""
    tenant, sub, b = ctx["tenant"], ctx.get("sub"), ctx.get("body") or {}
    cap = b.get("capability") or "residential_density"
    persons = round(sum(float(p.get("persons") or 0) for p in ctx["kfind"]("welfare.priority")), 0)
    if persons <= 0: persons = float(b.get("persons_below") or 0)
    rid = f"ds:{tenant}:welfare/need_rollup/{cap}"
    ctx["kwrite"](rid, {"schema": "welfare.need_rollup", "id": rid, "tenant": tenant, "level": b.get("level", "city"),
        "jurisdiction": b.get("jurisdiction"), "capability": cap, "persons_below": persons, "unit": "persons",
        "sources": [tenant], "observed_at": _now(),
        "prov": {"method": "aggregate of welfare.priority cells below floor", "actor": sub}})
    return {"rollup": rid, "level": b.get("level", "city"), "persons_below": persons}, None

def h_need_rollup(ctx):
    """Run by STATE / MINISTRY: sum children's rollups across tenant boundaries (aggregator lane) → sized envelope input."""
    tenant, sub, b = ctx["tenant"], ctx.get("sub"), ctx.get("body") or {}
    cap = b.get("capability") or "residential_density"; children = b.get("children") or []; level = b.get("level", "state")
    kx = ctx.get("kfind_x"); total = 0.0; srcs = []
    for ch in children:
        for r in (kx("welfare.need_rollup", ch, {"capability": cap}) if kx else []):
            total += float(r.get("persons_below") or 0); srcs.append(r["id"])
    rid = f"ds:{tenant}:welfare/need_rollup/{cap}"
    ctx["kwrite"](rid, {"schema": "welfare.need_rollup", "id": rid, "tenant": tenant, "level": level,
        "jurisdiction": b.get("jurisdiction"), "capability": cap, "persons_below": round(total, 0), "unit": "persons",
        "sources": srcs, "observed_at": _now(), "prov": {"method": f"sum of {len(children)} child rollups (aggregator lane)", "actor": sub}})
    return {"rollup": rid, "level": level, "persons_below": round(total, 0), "children": children, "sources": len(srcs)}, None

def h_monitor(ctx):
    """The funder's monitoring view: for each project it funds, its progress SUBSCRIPTION (read cross-tenant from
    the implementer) + the milestone VCs it holds (verified offline). Powers the Funder Console monitoring panel."""
    b = ctx.get("body") or {}; funder = b.get("funder") or f"ds:{ctx['tenant']}:agency/{ctx['tenant']}"
    kx = ctx.get("kfind_x"); net = ctx.get("network_url"); tok = ctx.get("token")
    projects = {}
    for a in ctx["kfind"]("funding.award"):                   # this funder's own awards → the projects it funds
        p = a.get("project")
        if p and p not in projects:
            projects[p] = {"project": p, "proposal": a.get("proposal"), "amount_cr": a.get("amount_cr"),
                           "released_cr": a.get("released_cr"), "status": a.get("status")}
    out = []
    for p, info in projects.items():
        pt = _tenant_of(p, ctx["tenant"])
        grants = (kx("funding.progress_grant", pt, {"project": p}) if (kx and pt != ctx["tenant"]) else ctx["kfind"]("funding.progress_grant", {"project": p}))
        sub = next((g for g in grants if _tenant_of(g.get("subscriber"), "") == ctx["tenant"] or g.get("subscriber") == funder), None)
        vcs = []
        for vc in _milestone_vcs(ctx, p):
            ver = False
            try: ver = bool(ctx["http"]("POST", net + "/v1/credentials/verify", {"credential": vc}, tok).get("verified"))
            except Exception: pass
            vcs.append({"id": vc["id"], "milestone": (vc.get("claims") or {}).get("milestone"), "verified": ver,
                        "issuer": vc.get("issuer_tenant"), "status": vc.get("status"), "issued_at": vc.get("issued_at")})
        out.append({**info, "provider": pt,
                    "subscription": ({"id": sub["id"], "status": sub.get("status"), "scope": sub.get("scope")} if sub else None),
                    "vcs": vcs})
    return {"funder": funder, "projects": out}, None

def h_commit_revenue(ctx):
    """Run by the IMPLEMENTER: roll the funder AWARDS into the project budget as COMMITTED revenue, and the
    receipts as REALIZED revenue — so the budget shows its expenditure plan is backed by funder commitments."""
    b = ctx.get("body") or {}; project = b.get("project"); funders = b.get("funders") or []
    if not project: return {"error": "project required"}, None
    awards = list(ctx["kfind"]("funding.award", {"project": project})); seen = {a["id"] for a in awards}
    kx = ctx.get("kfind_x")
    for tn in funders:
        if not kx or tn == ctx["tenant"]: continue
        for a in kx("funding.award", tn, {"project": project}):
            if a["id"] not in seen: awards.append(a); seen.add(a["id"])
    committed = round(sum(float(a.get("amount_cr") or 0) for a in awards), 2)
    realized = round(sum(float(t.get("amount") or 0) for t in ctx["kfind"]("finance.transaction")
                         if t.get("project") == project and t.get("direction") == "revenue"), 2)
    bud = next((x for x in ctx["kfind"]("finance.budget", {"project": project})), None)
    if not bud: return {"error": "no budget for project"}, None
    # reconcile the budget's allocation to the FUNDED proposal's ask (the one the awards reference) — the budget may
    # carry a stale earlier-version allocation. Default on; pass reconcile_allocated:false to keep the existing figure.
    ask = None; prop_id = next((a.get("proposal") for a in awards if a.get("proposal")), None)
    if prop_id:
        prop = ctx["kget"](prop_id) or (_read_proposal_x(ctx, prop_id))
        ask = ((prop or {}).get("ask") or {}).get("amount_cr")
    if ask and b.get("reconcile_allocated", True):
        bud["allocated"] = float(ask); bud["revenue_ask"] = float(ask)
    else:
        bud.setdefault("revenue_ask", bud.get("allocated"))
    bud["committed_revenue"] = committed; bud["realized_revenue"] = realized
    ctx["kwrite"](bud["id"], bud)
    return {"budget": bud["id"], "committed_revenue": committed, "realized_revenue": realized,
            "allocated": bud.get("allocated"), "reconciled_to_ask": (float(ask) if ask else None), "awards": len(awards),
            "funded": committed >= float(bud.get("allocated") or 0) > 0}, None

HANDLERS = {"funding-publish-program": h_publish_program, "funding-award": h_award,
            "funding-stack": h_stack, "funding-release-tranche": h_release_tranche,
            "funding-record-receipt": h_record_receipt, "funding-received": h_received,
            "funding-subscribe": h_subscribe, "funding-revoke-subscription": h_revoke_subscription,
            "funding-milestone": h_milestone, "need-publish": h_need_publish, "need-rollup": h_need_rollup,
            "funding-monitor": h_monitor, "funding-commit-revenue": h_commit_revenue}

def _selftest():
    store = {}; prop = {"id": "ds:x:project/proposal/p--v2", "project": "ds:x:project/p", "ask": {"amount_cr": 180}}
    db = {"project.proposal": [prop], "project.outcome": [], "finance.budget": [{"id": "ds:x:finance/budget/b", "project": "ds:x:project/p"}]}
    def kfind(s, where=None):
        rows = db.get(s, []) + [v for v in store.values() if v.get("schema") == s]
        return [r for r in rows if not where or all(r.get(k) == v for k, v in where.items())]
    def kget(i): return store.get(i) or next((r for r in db.get("project.proposal", []) if r["id"] == i), None)
    posted = {"n": 0}
    def call(t, v, p): posted["n"] += 1; return {"posted": f"ds:x:finance/transaction/t{posted['n']}"}, None
    ctx = {"tenant": "x", "sub": "mgr", "kwrite": lambda i, o: store.__setitem__(i, o), "kfind": kfind, "kget": kget, "call": call}
    pr, _ = h_publish_program(dict(ctx, body={"funder": "MoHUA", "title": "PMAY-U", "envelope_cr": 50000, "co_funders": [{"funder": "MoHUA", "share": 0.6}, {"funder": "HUDD", "share": 0.4}]}))
    a1, _ = h_award(dict(ctx, body={"program": pr["program"], "proposal": prop["id"], "funder": "MoHUA", "share": 0.6, "amount_cr": 108}))
    a2, _ = h_award(dict(ctx, body={"program": pr["program"], "proposal": prop["id"], "funder": "HUDD", "share": 0.4, "amount_cr": 72}))
    assert a2["funded"] and a2["committed_cr"] == 180, a2
    db["funding.progress_grant"] = []
    b0, _ = h_release_tranche(dict(ctx, body={"award": a1["award"], "amount_cr": 54}))
    assert b0.get("check") == "subscription", b0                          # no subscription → refused
    h_subscribe(dict(ctx, body={"subscriber": "x", "project": "ds:x:project/p"}))
    b1, _ = h_release_tranche(dict(ctx, body={"award": a1["award"], "amount_cr": 54}))
    assert b1.get("gate") == "BLOCKED", b1                                # subscribed but no milestone → refused
    db["project.outcome"].append({"id": "ds:x:project/outcome/p", "project": "ds:x:project/p"})
    ok, _ = h_release_tranche(dict(ctx, body={"award": a1["award"], "amount_cr": 54}))
    assert ok.get("gate") == "OPEN" and ok["transaction"] and store[a1["award"]]["released_cr"] == 54, ok
    print("selftest OK — stack", a2["committed_cr"], "| gate: no-sub→sub-no-milestone→open | released", ok["released"])

def main():
    print("Funding Engine — handlers:", ", ".join(sorted(HANDLERS))); _selftest(); return 0

if __name__ == "__main__":
    raise SystemExit(main())
