#!/usr/bin/env python3
"""Decision Assurance — reconstruct (defend) and contest a signed decision.

decision-dossier: given a governance.decision id, assemble the full accountable record — authority, rationale,
signature, the recommendations in its evidence_manifest and the governed FACTS each one cited (with method and
freshness so observed-vs-inferred is visible), and the measured outcome (intelligence.effectiveness). This is the
"what was knowable when we decided, and what happened after" view.
contest-decision: file a challenge against a decision — write a platform.inbox_item to the deciding authority for
review, and return the dossier so the challenger sees the exact basis. Contestability by design. In-process engine.
"""
import os as _os, json as _json, time as _t
def _now(): return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())


def _dossier(ctx, did):
    kfind = ctx["kfind"]
    decs = {d.get("id"): d for d in kfind("governance.decision")}
    d = decs.get(did)
    if not d:
        return None
    recs = {r.get("id"): r for r in kfind("governance.recommendation")}
    # index the governed facts the recommendations cite, by schema
    factidx = {}
    for sch in ("risk.flood", "cell.crowd_density", "vision.detection"):
        for o in kfind(sch):
            factidx[o.get("id")] = (sch, o)
    manifest = []
    for rid in (d.get("evidence_manifest") or d.get("considered") or []):
        r = recs.get(rid)
        if not r:
            continue
        cited = []
        for ev in (r.get("evidence") or []):
            sch, o = factidx.get(ev, (None, None))
            if o:
                cited.append({"id": ev, "schema": sch, "method": o.get("method"),
                              "observed_at": o.get("observed_at"),
                              "value": o.get("score") if sch == "risk.flood" else (o.get("density_sqm") or o.get("count"))})
            else:
                cited.append({"id": ev, "schema": None, "note": "referenced fact not resolvable in current tenant view"})
        manifest.append({"recommendation": rid, "title": r.get("title"), "proposed_action": r.get("proposed_action"),
                         "method": r.get("method"), "confidence": r.get("confidence"), "cited_facts": cited})
    eff = [e for e in kfind("intelligence.effectiveness") if e.get("decision") == did]
    sig = d.get("signature") or {}
    return {
        "decision": {"id": did, "authority": d.get("authority"), "outcome": d.get("outcome"),
                     "rationale": d.get("rationale"), "decided_at": d.get("decided_at"), "case": d.get("case")},
        "signature": {"suite": sig.get("suite"), "key_id": sig.get("key_id"), "signed": bool(sig.get("value")),
                      "verify": "offline via the network key directory"},
        "reconstructed_as_of": d.get("decided_at"),
        "evidence_manifest": manifest,
        "outcome": ([{"direction": e.get("direction"), "delta_pct": e.get("delta_pct"),
                      "baseline": (e.get("baseline") or {}).get("mean"), "followup": (e.get("followup") or {}).get("mean"),
                      "note": e.get("note")} for e in eff] or None),
        "note": ("Reconstructed from the governed records as of the decision time. Signature is verifiable offline "
                 "against the network key directory; later corrections are recorded separately and never rewrite this record."),
    }


def h_dossier(ctx):
    did = (ctx.get("body") or {}).get("decision")
    if not did:
        return None, "decision id required"
    d = _dossier(ctx, did)
    return (d, None) if d else (None, f"decision not found: {did}")


def h_contest(ctx):
    body = ctx.get("body") or {}; did = body.get("decision")
    if not did:
        return None, "decision id required"
    d = _dossier(ctx, did)
    if not d:
        return None, f"decision not found: {did}"
    tenant = ctx["tenant"]; reason = (body.get("reason") or "").strip() or "(no reason given)"
    by = body.get("by") or ctx.get("sub") or "user://unknown"
    authority = d["decision"].get("authority") or "user://unknown"
    iid = f"ds:{tenant}:inbox/contest-{did.rsplit('/',1)[-1]}"
    ctx["kwrite"](iid, {"schema": "platform.inbox_item", "id": iid, "tenant": tenant, "actor": authority,
        "kind": "decision", "title": f"Decision contested: {d['decision'].get('outcome')} — {did.rsplit('/',1)[-1]}",
        "body": f"Challenged by {by}. Reason: {reason}. Review the dossier (evidence + rationale + outcome) and uphold, amend, or reverse.",
        "ref": did, "source": "component://ds:airawatdeveloper:engine/decision-assurance", "status": "unread", "created_at": _now()})
    return {"contested": did, "routed_to": authority, "reason": reason, "dossier": d}, None


HANDLERS = {"decision-dossier": h_dossier, "contest-decision": h_contest}
if __name__ == "__main__":
    print("Decision Assurance — verbs: decision-dossier (defend), contest-decision (challenge).")
