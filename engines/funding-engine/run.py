"""Funding Engine (aos SDK rewrite) — the information-gates-money loop for a funding agency (ADR-0072 funder side).

publish-program → award (stacking shares) → stack check (Σ awards ≥ ask) → GATED tranche release (only when a
verified milestone exists). Release posts a revenue finance.transaction into the project ledger via finance-engine.
The rule the whole design turns on: money cannot move until the information (a verified milestone) moves.

Faithful, behaviourally-identical rewrite of components/engines/funding-engine onto the `aos` SDK:
registry/broker I/O moves to `ctx` (facts/get/write/call); external network egress (the ADR-0049 credential
service) stays plain urllib with URL+token read from env in-body; error-carrying result dicts are returned
verbatim (the original never returned a (None, err) — every return was (result, None)).

The cross-tenant aggregator lane (ADR-0072) uses `ctx.facts_x(schema, tenant)` — the SDK's host `kfind_x`
capability. Where the host does not grant it (`ctx.can("kfind_x")` is False), every cross-tenant branch
degrades to the own-tenant path, exactly as before. External network egress (ADR-0049 credential service)
stays plain urllib with URL+token from env in-body (host network token/url is a separate lane).
"""
import os, time as _t, json as _json, urllib.request, urllib.parse as _up
from aos import engine, ctx

CID = "ds:airawatdeveloper:engine/funding-engine"
FIN = "ds:airawatdeveloper:engine/finance-engine"


def _now():
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())


def _slug(s):
    return str(s).split("/")[-1].replace(":", "-")


def _tenant_of(oid, default):
    return oid.split(":")[1] if isinstance(oid, str) and oid.startswith("ds:") and len(oid.split(":")) > 2 else default


def _ask(prop):
    return float(((prop or {}).get("ask") or {}).get("amount_cr") or 0)


def _kx():
    """Cross-tenant registry read (aggregator lane, ADR-0072) via ctx.facts_x — the SDK's host kfind_x
    capability. Returns a (schema, tenant, where)->rows reader when the host granted it, else None → the
    own-tenant fallback (unchanged behaviour where the lane isn't available)."""
    if not ctx.can("kfind_x"):
        return None
    return lambda schema, tenant, where=None: ctx.facts_x(schema, tenant, where).list()


# ── external network egress (ADR-0049 credential service): plain urllib, URL+token from env in-body ──
def _net():
    return os.environ.get("AIRAWAT_NETWORK") or os.environ.get("AIRAWAT_NETWORK_URL")


def _tok():
    return os.environ.get("RUN_TOKEN")


def _http(method, url, payload, tok):
    data = _json.dumps(payload).encode() if payload is not None else None
    hdr = {"Content-Type": "application/json"}
    if tok:
        hdr["Authorization"] = "Bearer " + tok
        hdr["X-Run-Token"] = tok
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    with urllib.request.urlopen(req, timeout=90) as r:
        return _json.loads(r.read() or b"{}")


def _proposal(pid):
    return ctx.get(pid, "project.proposal")


def _read_proposal_x(proposal):
    """Read the proposal — own tenant, else cross-tenant (the funder reads the implementer's proposal)."""
    prop = ctx.get(proposal, "project.proposal")
    kx = _kx()
    if not prop and kx:
        rows = kx("project.proposal", _tenant_of(proposal, ctx.tenant), {"id": proposal})
        prop = rows[0] if rows else None
    return prop


def _stack(proposal, prop=None, funders=None):
    prop = prop or _read_proposal_x(proposal)
    ask = _ask(prop)
    awards = list(ctx.facts("funding.award").where(lambda a: a.get("proposal") == proposal))   # own-tenant awards
    seen = {a["id"] for a in awards}
    kx = _kx()
    for tn in (funders or []):                                               # + each funder tenant's awards (aggregator lane)
        if not kx or tn == ctx.tenant:
            continue
        for a in kx("funding.award", tn, {"proposal": proposal}):
            if a["id"] not in seen:
                awards.append(a); seen.add(a["id"])
    committed = round(sum(float(a.get("amount_cr") or 0) for a in awards), 2)
    return {"ask_cr": ask, "committed_cr": committed, "gap_cr": round(max(0.0, ask - committed), 2),
            "funded": committed >= ask > 0, "awards": len(awards), "_awards": awards}


@engine.verb("funding-publish-program", implements="funding.publish_program@0.1.0", writes=["funding.program"])
def h_publish_program(body):
    tenant, sub, b = ctx.tenant, ctx.sub, body or {}
    funder = b.get("funder"); title = b.get("title"); env = b.get("envelope_cr")
    if not funder or not title or env is None:
        return {"error": "funder + title + envelope_cr required"}
    scheme = b.get("scheme") or title
    pid = f"ds:{tenant}:funding/program/{_slug(b.get('program_id') or scheme)}"
    rec = {"schema": "funding.program", "id": pid, "tenant": tenant, "funder": funder, "title": title,
           "description": b.get("description"), "scheme": scheme, "envelope_cr": float(env),
           "currency": b.get("currency", "INR"), "co_funders": b.get("co_funders"),
           "eligibility": b.get("eligibility"), "reporting": b.get("reporting") or {"milestone_gated": True},
           "status": "open", "created_at": _now(),
           "prov": {"method": "funder programme publication", "actor": sub}}
    ctx.write(rec)
    return {"program": pid, "funder": funder, "envelope_cr": env, "co_funders": b.get("co_funders")}


@engine.verb("funding-award", implements="funding.award@0.1.0",
             reads=["project.proposal", "funding.award"], writes=["funding.award"])
def h_award(body):
    tenant, sub, b = ctx.tenant, ctx.sub, body or {}
    program = b.get("program"); proposal = b.get("proposal"); funder = b.get("funder"); amt = b.get("amount_cr")
    if not program or not proposal or not funder or amt is None:
        return {"error": "program + proposal + funder + amount_cr required"}
    prop = _proposal(proposal)
    project = b.get("project") or (prop or {}).get("project")
    aid = f"ds:{tenant}:funding/award/{_slug(proposal)}-{_slug(funder)}"
    rec = {"schema": "funding.award", "id": aid, "tenant": tenant, "program": program, "proposal": proposal,
           "project": project, "funder": funder, "share": b.get("share"), "amount_cr": float(amt),
           "currency": b.get("currency", "INR"), "status": "committed", "released_cr": 0.0,
           "decision": b.get("decision"), "awarded_at": _now(),
           "prov": {"method": "funder sanction → signed commitment", "actor": sub}}
    ctx.write(rec)
    stk = _stack(proposal, prop); stk.pop("_awards", None)
    return {"award": aid, "funder": funder, "amount_cr": amt, **stk}


@engine.verb("funding-stack", implements="funding.stack@0.1.0", reads=["project.proposal", "funding.award"])
def h_stack(body):
    b = body or {}; proposal = b.get("proposal")
    if not proposal:
        return {"error": "proposal required"}
    s = _stack(proposal, None, b.get("funders"))
    awards = s.pop("_awards")
    return {"proposal": proposal, **s,
            "breakdown": [{"funder": a.get("funder"), "tenant": _tenant_of(a.get("id"), ""), "amount_cr": a.get("amount_cr"),
                           "share": a.get("share"), "status": a.get("status"), "released_cr": a.get("released_cr")}
                          for a in awards]}


@engine.verb("funding-release-tranche", implements="funding.release_tranche@0.1.0",
             reads=["funding.award", "project.outcome", "funding.progress_grant", "finance.account"],
             writes=["funding.award"])
def h_release_tranche(body):
    tenant, sub, b = ctx.tenant, ctx.sub, body or {}
    award_id = b.get("award"); amt = b.get("amount_cr")
    if not award_id or amt is None:
        return {"error": "award + amount_cr required"}
    award = ctx.get(award_id, "funding.award")
    if not award:
        return {"error": f"award '{award_id}' not found"}
    project = award.get("project")

    # THE GATE — information gates money. Two governed checks (ADR-0072 S3c/d):
    #   (1) a STANDING progress subscription authorises this funder to read the project (revocable, not the blanket grant)
    #   (2) a MILESTONE VC (ADR-0049) verifies against the implementer's published key — a signed proof, not a raw read
    ptenant = _tenant_of(project, ctx.tenant); kx = _kx()
    funder_tenant = _tenant_of(award.get("funder"), None) or _tenant_of(award_id, ctx.tenant)
    sub = _active_subscription(funder_tenant, project, ptenant)
    if not sub:
        return {"error": "no active progress subscription for this funder+project (revoked or never granted)",
                "gate": "BLOCKED", "check": "subscription"}
    verified, basis = _verify_milestone_vc(project)
    if not verified:                                          # fallback: the outcome proxy (back-compat) still counts
        oc = (kx("project.outcome", ptenant, {"project": project}) if (ptenant != ctx.tenant and kx)
              else ctx.facts("project.outcome").where(lambda o: o.get("project") == project).list())
        verified = bool(oc); basis = (oc[0]["id"] + " (outcome proxy)") if oc else None
    if not verified:
        return {"error": "information-gates-money: no verified milestone for this release", "gate": "BLOCKED",
                "hint": "record + verify a milestone (project.outcome / milestone_ref) before releasing"}

    # release ≤ remaining commitment
    remaining = float(award.get("amount_cr") or 0) - float(award.get("released_cr") or 0)
    amt = min(float(amt), remaining)
    if amt <= 0:
        return {"error": "nothing left to release on this award", "released_cr": award.get("released_cr")}

    # the funder posts its DISBURSEMENT (grant expenditure) in ITS OWN ledger (money leaving the funder tenant)
    exp = b.get("expense_account") or next((a["id"] for a in ctx.facts("finance.account") if a.get("type") == "expense"), None)
    res, err = ctx.call(FIN, "finance-post", {
        "direction": "expenditure", "amount": amt, "account": exp, "contra_type": "liability",
        "project": project, "memo": f"funding tranche: {award.get('funder')} → {_slug(award.get('proposal'))}",
        "source": award_id, "source_kind": "funding.award", "status": "posted"})
    if err:
        return {"error": f"finance posting failed: {err}", "gate": "OPEN"}

    award["released_cr"] = round(float(award.get("released_cr") or 0) + amt, 2)
    award["status"] = "released" if award["released_cr"] >= float(award.get("amount_cr") or 0) - 1e-6 else "partially-released"
    ctx.write(award)
    return {"released": amt, "award": award_id, "funder": award.get("funder"), "gate": "OPEN",
            "verified_by": basis, "transaction": res.get("posted"), "award_released_cr": award["released_cr"],
            "award_status": award["status"]}


def _read_award_x(award_id):
    at = _tenant_of(award_id, ctx.tenant)
    if at == ctx.tenant:
        return ctx.get(award_id, "funding.award")
    kx = _kx()
    rows = kx("funding.award", at, {"id": award_id}) if kx else []
    return rows[0] if rows else None


@engine.verb("funding-record-receipt", implements="funding.record_receipt@0.1.0",
             reads=["funding.award", "finance.transaction", "finance.account"])
def h_record_receipt(body):
    """Run by the IMPLEMENTER: book the funder's released tranche as REVENUE in its OWN ledger, reconciled against
    the funder's signed award (read cross-tenant). Two sets of books, one shared award — closes the money loop."""
    tenant, sub, b = ctx.tenant, ctx.sub, body or {}
    award_id = b.get("award")
    if not award_id:
        return {"error": "award required"}
    award = _read_award_x(award_id)
    if not award:
        return {"error": f"award '{award_id}' not found"}
    released = float(award.get("released_cr") or 0); project = award.get("project")
    prior = round(sum(float(t.get("amount") or 0) for t in ctx.facts("finance.transaction")
                      if t.get("source") == award_id and t.get("direction") == "revenue"), 2)
    newly = round(released - prior, 2)
    if newly <= 0:
        return {"received": 0.0, "total_received_cr": prior, "note": "ledger already up to date with the award"}
    rev = b.get("revenue_account") or next((a["id"] for a in ctx.facts("finance.account") if a.get("type") in ("revenue", "income")), None)
    res, err = ctx.call(FIN, "finance-post", {
        "direction": "revenue", "amount": newly, "account": rev, "contra_type": "asset",
        "project": project, "memo": f"funding received: {award.get('funder')} tranche", "source": award_id,
        "source_kind": "funding.award", "status": "posted"})
    if err:
        return {"error": f"receipt posting failed: {err}"}
    return {"received": newly, "total_received_cr": released, "funder": award.get("funder"),
            "transaction": res.get("posted"), "project": project}


@engine.verb("funding-received", implements="funding.received@0.1.0", reads=["finance.transaction"])
def h_received(body):
    b = body or {}; project = b.get("project")
    if not project:
        return {"error": "project required"}
    rev = [t for t in ctx.facts("finance.transaction") if t.get("project") == project and t.get("direction") == "revenue"]
    total = round(sum(float(t.get("amount") or 0) for t in rev), 2)
    return {"project": project, "received_cr": total, "receipts": len(rev),
            "by_source": [{"award": t.get("source"), "amount_cr": t.get("amount"), "at": t.get("date")} for t in rev]}


# ---------------------------------------------------------------- S3c: progress subscription (standing, revocable)
def _active_subscription(funder_tenant, project, ptenant=None):
    ptenant = ptenant or _tenant_of(project, ctx.tenant); kx = _kx()
    grants = (kx("funding.progress_grant", ptenant, {"project": project}) if (ptenant != ctx.tenant and kx)
              else ctx.facts("funding.progress_grant").where(lambda g: g.get("project") == project).list())
    for g in grants:
        if g.get("status") == "active" and (_tenant_of(g.get("subscriber"), "") == funder_tenant
                                            or g.get("subscriber") == funder_tenant):
            return g
    return None


@engine.verb("funding-subscribe", implements="funding.subscribe@0.1.0", writes=["funding.progress_grant"])
def h_subscribe(body):
    """Run by the IMPLEMENTER: grant a funder a standing, revocable read of one project's progress."""
    tenant, sub, b = ctx.tenant, ctx.sub, body or {}
    subscriber = b.get("subscriber"); project = b.get("project")
    if not subscriber or not project:
        return {"error": "subscriber + project required"}
    gid = f"ds:{tenant}:funding/progress_grant/{_slug(project)}-{_slug(subscriber)}"
    ctx.write({"schema": "funding.progress_grant", "id": gid, "tenant": tenant, "subscriber": subscriber,
               "project": project, "provider": tenant, "status": "active", "issued_by": sub, "issued_at": _now(),
               "scope": b.get("scope") or ["project.outcome", "project.output", "finance.transaction", "credential.milestone"]})
    return {"subscription": gid, "subscriber": subscriber, "project": project, "status": "active"}


@engine.verb("funding-revoke-subscription", implements="funding.revoke_subscription@0.1.0",
             reads=["funding.progress_grant"], writes=["funding.progress_grant"])
def h_revoke_subscription(body):
    b = body or {}; gid = b.get("subscription"); g = ctx.get(gid, "funding.progress_grant") if gid else None
    if not g:
        return {"error": "subscription not found"}
    g["status"] = "revoked"; g["revoked_at"] = _now(); ctx.write(g)
    return {"revoked": gid, "status": "revoked"}


# ---------------------------------------------------------------- S3d: milestone as a real ADR-0049 VC
def _milestone_vcs(project):
    """All credential.milestone VCs the implementer (project's tenant) issued for this project — holder-agnostic,
    so the funder verifies by the ISSUER's key regardless of which officer holds the credential."""
    tok = _tok(); net = _net()
    if not net:
        return []
    try:
        lst = _http("GET", net + "/v1/credentials?" + _up.urlencode({"issuer_tenant": _tenant_of(project, ctx.tenant), "type": "credential.milestone"}), None, tok)
    except Exception:
        return []
    return [vc for vc in (lst.get("credentials") or []) if (vc.get("claims") or {}).get("project") == project]


def _verify_milestone_vc(project):
    net = _net(); tok = _tok()
    for vc in _milestone_vcs(project):
        try:
            if _http("POST", net + "/v1/credentials/verify", {"credential": vc}, tok).get("verified"):
                return True, vc["id"] + " (VC ✓)"
        except Exception:
            pass
    return False, None


@engine.verb("funding-milestone", implements="funding.milestone@0.1.0", reads=["project.outcome"])
def h_milestone(body):
    """Run by the IMPLEMENTER: issue a signed credential.milestone VC to each holder funder (ADR-0049)."""
    tenant, b = ctx.tenant, body or {}
    project = b.get("project"); milestone = b.get("milestone") or "m1"; holders = b.get("holders") or []
    tok = _tok(); net = _net()
    oc = [o for o in ctx.facts("project.outcome").where(lambda o: o.get("project") == project)]
    claims = {"project": project, "milestone": milestone, "outcome_ref": (oc[0]["id"] if oc else None),
              "persons_moved": (oc[0].get("persons_moved") if oc else None)}
    issued = []
    for h in holders:
        try:
            r = _http("POST", net + "/v1/credentials/issue", {"issuer_tenant": tenant, "holder": h,
                "type": "credential.milestone", "title": f"Milestone {milestone} verified", "claims": claims,
                "subject_ref": project}, tok)
            issued.append({"holder": h, "vc": r.get("issued")})
        except Exception as e:
            issued.append({"holder": h, "error": str(e)[:120]})
    return {"project": project, "milestone": milestone, "issued": issued}


# ---------------------------------------------------------------- flow ②: need aggregation up the ladder
@engine.verb("need-publish", reads=["welfare.priority"], writes=["welfare.need_rollup"])
def h_need_publish(body):
    """Run by a CITY: publish its aggregate welfare need (persons below the floor) — only the aggregate, raw stays local."""
    tenant, sub, b = ctx.tenant, ctx.sub, body or {}
    cap = b.get("capability") or "residential_density"
    persons = round(sum(float(p.get("persons") or 0) for p in ctx.facts("welfare.priority")), 0)
    if persons <= 0:
        persons = float(b.get("persons_below") or 0)
    rid = f"ds:{tenant}:welfare/need_rollup/{cap}"
    ctx.write({"schema": "welfare.need_rollup", "id": rid, "tenant": tenant, "level": b.get("level", "city"),
               "jurisdiction": b.get("jurisdiction"), "capability": cap, "persons_below": persons, "unit": "persons",
               "sources": [tenant], "observed_at": _now(),
               "prov": {"method": "aggregate of welfare.priority cells below floor", "actor": sub}})
    return {"rollup": rid, "level": b.get("level", "city"), "persons_below": persons}


@engine.verb("need-rollup", reads=["welfare.need_rollup"], writes=["welfare.need_rollup"])
def h_need_rollup(body):
    """Run by STATE / MINISTRY: sum children's rollups across tenant boundaries (aggregator lane) → sized envelope input."""
    tenant, sub, b = ctx.tenant, ctx.sub, body or {}
    cap = b.get("capability") or "residential_density"; children = b.get("children") or []; level = b.get("level", "state")
    kx = _kx(); total = 0.0; srcs = []
    for ch in children:
        for r in (kx("welfare.need_rollup", ch, {"capability": cap}) if kx else []):
            total += float(r.get("persons_below") or 0); srcs.append(r["id"])
    rid = f"ds:{tenant}:welfare/need_rollup/{cap}"
    ctx.write({"schema": "welfare.need_rollup", "id": rid, "tenant": tenant, "level": level,
               "jurisdiction": b.get("jurisdiction"), "capability": cap, "persons_below": round(total, 0), "unit": "persons",
               "sources": srcs, "observed_at": _now(), "prov": {"method": f"sum of {len(children)} child rollups (aggregator lane)", "actor": sub}})
    return {"rollup": rid, "level": level, "persons_below": round(total, 0), "children": children, "sources": len(srcs)}


@engine.verb("funding-monitor", implements="funding.monitor@0.1.0",
             reads=["funding.award", "funding.progress_grant"])
def h_monitor(body):
    """The funder's monitoring view: for each project it funds, its progress SUBSCRIPTION (read cross-tenant from
    the implementer) + the milestone VCs it holds (verified offline). Powers the Funder Console monitoring panel."""
    b = body or {}; funder = b.get("funder") or f"ds:{ctx.tenant}:agency/{ctx.tenant}"
    kx = _kx(); net = _net(); tok = _tok()
    projects = {}
    for a in ctx.facts("funding.award"):                      # this funder's own awards → the projects it funds
        p = a.get("project")
        if p and p not in projects:
            projects[p] = {"project": p, "proposal": a.get("proposal"), "amount_cr": a.get("amount_cr"),
                           "released_cr": a.get("released_cr"), "status": a.get("status")}
    out = []
    for p, info in projects.items():
        pt = _tenant_of(p, ctx.tenant)
        grants = (kx("funding.progress_grant", pt, {"project": p}) if (kx and pt != ctx.tenant)
                  else ctx.facts("funding.progress_grant").where(lambda g: g.get("project") == p).list())
        sub = next((g for g in grants if _tenant_of(g.get("subscriber"), "") == ctx.tenant or g.get("subscriber") == funder), None)
        vcs = []
        for vc in _milestone_vcs(p):
            ver = False
            try:
                ver = bool(_http("POST", net + "/v1/credentials/verify", {"credential": vc}, tok).get("verified"))
            except Exception:
                pass
            vcs.append({"id": vc["id"], "milestone": (vc.get("claims") or {}).get("milestone"), "verified": ver,
                        "issuer": vc.get("issuer_tenant"), "status": vc.get("status"), "issued_at": vc.get("issued_at")})
        out.append({**info, "provider": pt,
                    "subscription": ({"id": sub["id"], "status": sub.get("status"), "scope": sub.get("scope")} if sub else None),
                    "vcs": vcs})
    return {"funder": funder, "projects": out}


@engine.verb("funding-commit-revenue", implements="funding.commit_revenue@0.1.0",
             reads=["funding.award", "finance.transaction", "finance.budget", "project.proposal"],
             writes=["finance.budget"])
def h_commit_revenue(body):
    """Run by the IMPLEMENTER: roll the funder AWARDS into the project budget as COMMITTED revenue, and the
    receipts as REALIZED revenue — so the budget shows its expenditure plan is backed by funder commitments."""
    b = body or {}; project = b.get("project"); funders = b.get("funders") or []
    if not project:
        return {"error": "project required"}
    awards = list(ctx.facts("funding.award").where(lambda a: a.get("project") == project)); seen = {a["id"] for a in awards}
    kx = _kx()
    for tn in funders:
        if not kx or tn == ctx.tenant:
            continue
        for a in kx("funding.award", tn, {"project": project}):
            if a["id"] not in seen:
                awards.append(a); seen.add(a["id"])
    committed = round(sum(float(a.get("amount_cr") or 0) for a in awards), 2)
    realized = round(sum(float(t.get("amount") or 0) for t in ctx.facts("finance.transaction")
                         if t.get("project") == project and t.get("direction") == "revenue"), 2)
    bud = next((x for x in ctx.facts("finance.budget").where(lambda x: x.get("project") == project)), None)
    if not bud:
        return {"error": "no budget for project"}
    # reconcile the budget's allocation to the FUNDED proposal's ask (the one the awards reference) — the budget may
    # carry a stale earlier-version allocation. Default on; pass reconcile_allocated:false to keep the existing figure.
    ask = None; prop_id = next((a.get("proposal") for a in awards if a.get("proposal")), None)
    if prop_id:
        prop = ctx.get(prop_id, "project.proposal") or _read_proposal_x(prop_id)
        ask = ((prop or {}).get("ask") or {}).get("amount_cr")
    if ask and b.get("reconcile_allocated", True):
        bud["allocated"] = float(ask); bud["revenue_ask"] = float(ask)
    else:
        bud.setdefault("revenue_ask", bud.get("allocated"))
    bud["committed_revenue"] = committed; bud["realized_revenue"] = realized
    ctx.write(bud)
    return {"budget": bud["id"], "committed_revenue": committed, "realized_revenue": realized,
            "allocated": bud.get("allocated"), "reconciled_to_ask": (float(ask) if ask else None), "awards": len(awards),
            "funded": committed >= float(bud.get("allocated") or 0) > 0}


HANDLERS = engine.handlers()   # ← app_host finds this unchanged; SDK built it from the decorators
