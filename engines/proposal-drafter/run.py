"""Proposal Drafter (ADR-0071 S1) — recommendation → DRAFT project.proposal.

Given a governance.recommendation, gather the cell-grain welfare.priority behind it (worst-off first), the
capability/floor it concerns, and any signal.report ground-truth for those cells; pre-fill a results chain
(outcome: people above the floor; output: intervention across N cells), an indicative outlay, and the ADR-0071
funding-pivot fields (answers · evidence · site · intervention · ask). Then compose the project-engine's
proposal-submit so the existing baseline lifecycle (versioning, Inbox routing, decided-change-request →
governance.decision) is reused. AI pre-fills; the Junior Engineer confirms feasibility + picks the lever + budget.
"""
CID = "ds:airawatdeveloper:engine/proposal-drafter"
CVER = "0.1.0"
PROJECT_ENGINE = "ds:airawatdeveloper:engine/project-engine"

def _slug(s): return "".join(c if c.isalnum() else "-" for c in str(s)).strip("-").lower()[:48] or "x"

def h_proposal_draft(ctx):
    body, tenant = ctx.get("body") or {}, ctx["tenant"]
    rec_id = body.get("recommendation")
    if not rec_id:
        return None, "a recommendation id is required (body.recommendation)"
    top_n = int(body.get("top_n", 20))
    mode = body.get("mode", "open")                       # addressed | open | self
    org_unit = body.get("org_unit") or f"ds:{tenant}:org_unit/city-planning"

    recs = {r.get("id"): r for r in (ctx["kfind"]("governance.recommendation") or [])}
    rec = recs.get(rec_id)
    if not rec:
        return None, f"recommendation not found: {rec_id}"
    prios = [p for p in (ctx["kfind"]("welfare.priority") or []) if p.get("recommendation_ref") == rec_id]
    if not prios:
        return None, f"no welfare.priority cells reference {rec_id} — run cell-grain prioritisation first"
    prios.sort(key=lambda p: p.get("rank", 1e9))
    target = prios[:top_n]

    cap = target[0].get("capability")
    caps = {c.get("id"): c for c in (ctx["kfind"]("welfare.capability") or [])}
    floors = {f.get("capability"): f for f in (ctx["kfind"]("welfare.floor") or [])}
    cap_obj = caps.get(cap) or {}
    label = cap_obj.get("label") or (cap.split("/")[-1] if cap else "capability")
    unit = cap_obj.get("service_unit") or ""

    persons = sum(p.get("persons") or 0 for p in target)
    cost = sum(p.get("cost_cr") or 0 for p in target)
    env_res = target[0].get("env_resource")
    env = sum(p.get("env_draw") or 0 for p in target)
    cells = [(p.get("space") or {}).get("h3") for p in target if (p.get("space") or {}).get("h3")]
    funded_n = sum(1 for p in target if p.get("funded"))

    # ground-truth evidence is ATTACHED by the human (JE / citizen), not guessed: the caller passes the
    # report/document ids to bind. There is no governed citizen-report noun yet, so we do not invent one.
    evidence = body.get("evidence") or []

    # pick the recommended lever from the intervention catalogue (least environmental draw, then least cost)
    levers = [iv for iv in (ctx["kfind"]("welfare.intervention") or []) if iv.get("capability") == cap]
    _big = 9e9
    lever = sorted(levers, key=lambda x: (x.get("env_draw_per_person", _big) if x.get("env_draw_per_person") is not None else _big,
                                          x.get("unit_cost_cr_per_person", _big) if x.get("unit_cost_cr_per_person") is not None else _big))[0] if levers else None

    answers = [rec_id] + [p["id"] for p in target[:12]]
    project = {"code": f"{_slug(label)}-relief", "name": f"{label} relief — {tenant}", "org_unit": org_unit,
               "objective": f"Bring residents above the {label} dignity floor", "currency": "INR"}
    proposal = {
        "title": f"{label} relief — {len(target)} priority cells",
        "summary": (f"Relieve {label.lower()} for ~{round(persons):,} people across the {len(target)} worst-off cells "
                    f"flagged by the welfare prioritisation ({rec_id.split('/')[-1]}). Indicative outlay "
                    f"~₹{round(cost,1):,} cr. The Junior Engineer confirms feasibility, selects the intervention "
                    f"lever, and finalises the budget before submission."),
        "outcomes": [{"id": "o1", "statement": f"Residents brought above the {label} dignity floor",
                      "indicator": f"people below the floor ({unit})".strip(), "baseline": round(persons), "target": 0}],
        "outputs": [{"id": "y1", "statement": f"{label} intervention delivered across {len(target)} priority cells",
                     "outcome": "o1", "indicator": "priority cells treated", "target": len(target)}],
        "outlay": [{"id": "L1", "output": "y1", "resource_type": "works",
                    "description": f"{label} works across {len(target)} cells (INDICATIVE — replace with a cited DPR)",
                    "quantity": len(target), "unit": "cell", "rate": round(cost / max(len(target), 1), 4),
                    "amount": round(cost, 4)}],
        "answers": answers,
        "evidence": evidence,
        "site": {"jurisdiction": tenant, "cells": cells, "subject_kind": "cell"},
        "intervention": ({"kind": lever["kind"], "label": lever.get("label"), "description": lever.get("description"),
                          "unit_cost_cr_per_person": lever.get("unit_cost_cr_per_person"),
                          "env_resource": lever.get("env_resource"), "env_draw": round((lever.get("env_draw_per_person") or 0) * persons, 3),
                          "from_scheme": lever.get("from_scheme"), "from_catalog": True, "catalog_ref": lever.get("id")}
                         if lever else
                         {"kind": "(JE to select from the intervention catalog)", "description": f"Lever for {label}",
                          "unit_cost_cr": None, "env_resource": env_res, "env_draw": round(env, 3), "from_catalog": False}),
        "ask": {"amount_cr": round(cost, 3), "currency": "INR", "mode": mode, "program": body.get("program"),
                "beneficiaries": round(persons)},
    }
    # default: leave it as an editable DRAFT for the Junior Engineer (submit=false); pass submit=true to route now.
    as_draft = not bool(body.get("submit"))
    res, err = ctx["call"](PROJECT_ENGINE, "proposal-submit",
                           {"project": project, "proposal": proposal, "code": project["code"], "as_draft": as_draft})
    if err:
        return None, f"proposal-submit failed: {err}"
    return {"recommendation": rec_id, "capability": cap, "cells": len(target), "persons": round(persons),
            "ask_cr": round(cost, 2), "funded_now": funded_n, "evidence_reports": len(evidence),
            "proposal": (res or {}).get("proposal"), "project": (res or {}).get("project"),
            "routed_to": (res or {}).get("routed_to"), "status": (res or {}).get("status")}, None

HANDLERS = {"proposal-draft": h_proposal_draft}

def _selftest():
    prios = [{"id": f"ds:x:welfare/priority/density-c{i}", "capability": "welfare.capability/density",
              "recommendation_ref": "ds:x:governance/recommendation/welfare-cells-density", "rank": i,
              "persons": 1000 - i * 100, "cost_cr": 5.0 - i * 0.5, "env_resource": "land", "env_draw": 1.0 - i * 0.1,
              "funded": i <= 2, "space": {"h3": f"cell{i}"}} for i in range(1, 5)]
    caps = [{"id": "welfare.capability/density", "label": "Residential crowding", "service_unit": "persons_per_ha"}]
    floors = [{"capability": "welfare.capability/density", "threshold": 200}]
    recs = [{"id": "ds:x:governance/recommendation/welfare-cells-density", "title": "Raise crowding"}]
    captured = {}
    def call(tgt, verb, payload):
        captured["tgt"] = tgt; captured["verb"] = verb; captured["payload"] = payload
        return {"proposal": "ds:x:project/proposal/density-relief--v1", "project": "ds:x:project/density-relief",
                "routed_to": "role://x/platform.tenant-admin", "status": "submitted"}, None
    ctx = {"tenant": "x", "body": {"recommendation": "ds:x:governance/recommendation/welfare-cells-density",
                                   "top_n": 20, "evidence": ["ds:x:doc/site-photo-1"]},
           "call": call,
           "kfind": lambda s: {"governance.recommendation": recs, "welfare.priority": prios,
                               "welfare.capability": caps, "welfare.floor": floors}.get(s, [])}
    res, err = h_proposal_draft(ctx)
    assert err is None, err
    p = captured["payload"]["proposal"]
    assert captured["tgt"] == PROJECT_ENGINE and captured["verb"] == "proposal-submit"
    assert p["outcomes"] and p["outputs"], "results chain must be non-empty"
    assert p["answers"][0].endswith("welfare-cells-density") and len(p["answers"]) == 5, p["answers"]
    assert p["evidence"] == ["ds:x:doc/site-photo-1"], p["evidence"]      # attached by the caller (human)
    assert p["site"]["cells"] == ["cell1", "cell2", "cell3", "cell4"], p["site"]
    assert p["ask"]["mode"] == "open" and p["ask"]["beneficiaries"] == res["persons"], p["ask"]
    assert res["cells"] == 4 and res["funded_now"] == 2 and res["evidence_reports"] == 1, res
    print("selftest OK:", {k: res[k] for k in ("cells", "persons", "ask_cr", "funded_now", "evidence_reports")})
    print("  outcome:", p["outcomes"][0]["statement"], "| ask ₹cr:", p["ask"]["amount_cr"])

def main():
    print("Proposal Drafter — handlers:", ", ".join(sorted(HANDLERS)))
    _selftest(); return 0

if __name__ == "__main__":
    raise SystemExit(main())
