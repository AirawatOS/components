#!/usr/bin/env python3
"""Campaign Engine — run the field-verification campaign for tax-gap candidates: create a campaign, ASSIGN officers to
candidates, and record the on-site GROUND TRUTH → a governance.decision that verifies or dismisses each candidate.

The loop closes the tax-gap pipeline (AI flags, humans decide — ADR-0029): an assignment lands in the officer's Inbox as
an ACTIONABLE item ("Record verification" → verify-submit, with the ground-truth fields as inbox params), so the officer
completes it in one place. Completion writes a governance.decision (case=candidate, task=assignment), sets the candidate
verified|dismissed, closes the assignment + the Inbox task. Writes as component://campaign-engine obo the user.

Handlers: campaign-create · campaign-assign · verify-submit · campaign-progress.
"""
import time as _t, json as _json, uuid as _u

_j = _json.dumps
_ADMIN = {"platform.tenant-admin", "platform.steward", "admin"}


def _now():
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())


def h_campaign_create(ctx):
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    if not (set(ctx["claims"].get("roles", [])) & _ADMIN):
        return None, "creating a campaign requires a tenant admin"
    name = body.get("name")
    if not name:
        return None, "campaign name required"
    code = body.get("code") or name.lower().replace(" ", "-")[:24]
    cid = f"ds:{tenant}:campaign/campaign/{code}"
    now = _now()
    ctx["kwrite"](cid, {"schema": "campaign.campaign", "id": cid, "tenant": tenant, "code": code, "name": name,
        "ward": body.get("ward"), "area": body.get("area"), "status": "active", "description": body.get("description"),
        "created_by": sub, "target_count": body.get("target_count"), "created_at": now})
    return {"campaign": cid, "code": code, "status": "active"}, None


def h_campaign_assign(ctx):
    """Assign an officer to a candidate → an assignment + an ACTIONABLE Inbox item (Record verification)."""
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    if not (set(ctx["claims"].get("roles", [])) & _ADMIN):
        return None, "assigning requires a tenant admin"
    cand_id, officer, campaign = body.get("candidate"), body.get("officer"), body.get("campaign")
    if not cand_id or not officer:
        return None, "candidate and officer required"
    cand = ctx["kget"](cand_id)
    if not cand:
        return None, "candidate not found"
    officer = officer if str(officer).startswith("user://") else f"user://{tenant}/{str(officer).rsplit('/', 1)[-1]}"
    local = cand_id.rsplit("/", 1)[-1]
    aid = f"ds:{tenant}:campaign/assignment/{local}--{_u.uuid4().hex[:6]}"
    now = _now()
    ctx["kwrite"](aid, {"schema": "campaign.assignment", "id": aid, "tenant": tenant, "campaign": campaign,
        "candidate": cand_id, "parcel_id": cand.get("parcel_id"), "assignee": officer, "assigned_by": sub,
        "assigned_at": now, "status": "assigned", "created_at": now})
    ctx["kwrite"](cand_id, {**cand, "status": "assigned"})
    # actionable Inbox item for the officer — the ground-truth fields are inbox params on the action
    iid = f"ds:{tenant}:platform/inbox_item/assign-{aid.rsplit('/', 1)[-1]}"
    ctx["kwrite"](iid, {"schema": "platform.inbox_item", "id": iid, "tenant": tenant, "actor": officer,
        "title": f"Field verify — parcel {(cand.get('parcel_id') or '').rsplit('/', 1)[-1]}",
        "body": f"Tax-gap candidate (confidence {cand.get('confidence')}): " + ", ".join(e.get("signal", "") for e in (cand.get("evidence") or [])),
        "ref": cand_id, "kind": "task", "status": "unread", "created_at": now,
        "actions": [_j({"label": "Record verification", "verb": "verify-submit", "style": "pri",
            "args": {"assignment": aid},
            "params": [{"name": "outcome", "label": "Outcome", "type": "select", "options": ["confirmed", "not_taxable", "not_found"]},
                       {"name": "built_area", "label": "Built area (sqm)", "type": "number", "required": False},
                       {"name": "assessed_value", "label": "Assessed value (₹)", "type": "number", "required": False},
                       {"name": "notes", "label": "Notes", "type": "text", "required": False}]})]})
    return {"assignment": aid, "officer": officer, "candidate": cand_id, "routed_to": officer}, None


def h_verify_submit(ctx):
    """The officer records the on-site ground truth → a governance.decision that verifies/dismisses the candidate."""
    body, tenant, sub = ctx["body"], ctx["tenant"], ctx["sub"]
    aid = body.get("assignment")
    asg = ctx["kget"](aid) if aid else None
    if not asg:
        return None, "assignment not found"
    if asg.get("assignee") not in (sub, ctx["claims"].get("sub")):
        return None, "only the assigned officer may record this verification"
    outcome = body.get("outcome")
    if outcome not in ("confirmed", "not_taxable", "not_found"):
        return None, "outcome must be confirmed | not_taxable | not_found"
    now = _now()
    gt = {k: body.get(k) for k in ("built_area", "assessed_value", "occupancy", "notes") if body.get(k) is not None}
    gt["exists"] = outcome != "not_found"; gt["taxable"] = outcome == "confirmed"
    cand = ctx["kget"](asg["candidate"]) or {}
    did = f"ds:{tenant}:governance/decision/verify-{aid.rsplit('/', 1)[-1]}"
    ctx["kwrite"](did, {"schema": "governance.decision", "id": did, "tenant": tenant,
        "case": asg["candidate"], "task": aid, "outcome": "approved" if outcome == "confirmed" else "rejected",
        "authority": sub, "decided_at": now,
        "rationale": f"field verification: {outcome}" + (f" — {gt.get('notes')}" if gt.get("notes") else ""),
        "subjects": [asg["candidate"]], "considered": [asg["candidate"]] + ([cand["recommendation"]] if cand.get("recommendation") else []),
        "evidence_manifest": [asg["candidate"], aid]})
    if cand:
        ctx["kwrite"](cand["id"], {**cand, "status": "verified" if outcome == "confirmed" else "dismissed",
            "estimated_revenue": (body.get("assessed_value") if outcome == "confirmed" else cand.get("estimated_revenue"))})
    ctx["kwrite"](aid, {**asg, "status": "done", "outcome": outcome, "ground_truth": gt, "decision": did, "decided_at": now})
    prior = ctx["kget"](f"ds:{tenant}:platform/inbox_item/assign-{aid.rsplit('/', 1)[-1]}")
    if prior:
        ctx["kwrite"](prior["id"], {**prior, "status": "done"})
    return {"assignment": aid, "outcome": outcome, "candidate": asg["candidate"],
            "candidate_status": "verified" if outcome == "confirmed" else "dismissed", "decision": did}, None


def h_campaign_progress(ctx):
    tenant = ctx["tenant"]; camp = ctx["body"].get("campaign")
    asgs = [a for a in ctx["kfind"]("campaign.assignment") if (not camp or a.get("campaign") == camp)]
    by_status, by_outcome = {}, {}
    for a in asgs:
        by_status[a.get("status")] = by_status.get(a.get("status"), 0) + 1
        if a.get("outcome"):
            by_outcome[a["outcome"]] = by_outcome.get(a["outcome"], 0) + 1
    done = by_status.get("done", 0)
    return {"assignments": len(asgs), "by_status": by_status, "by_outcome": by_outcome,
            "completion_pct": round(100.0 * done / len(asgs), 1) if asgs else 0.0}, None


HANDLERS = {"campaign-create": h_campaign_create, "campaign-assign": h_campaign_assign,
            "verify-submit": h_verify_submit, "campaign-progress": h_campaign_progress}


def main():
    print("Campaign Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
