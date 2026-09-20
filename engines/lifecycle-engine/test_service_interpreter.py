"""Behavioral-parity test for the ported service interpreter (ADR-0097 step 1).
Loads lifecycle-engine.h_service_advance with a stub ctx and asserts it reproduces _advance_service_workflow's
logic exactly: multi-step advance (next task+inbox+case), issues-step VC instruction, final-step close, no-workflow.
No live stack — pure logic proof that the move engine<-host is behavior-preserving."""
import importlib.util, sys, json, os

spec = importlib.util.spec_from_file_location(
    "lce", os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py"))
lce = importlib.util.module_from_spec(spec); spec.loader.exec_module(lce)

def make_ctx(body, subm=None):
    writes = {}
    def kwrite(oid, obj): writes[oid] = obj; return {}
    def kget(oid): return subm if (subm and oid.endswith("submission/svcA-42")) else None
    return {"body": body, "tenant": "prov", "sub": "user://prov/officer1",
            "kwrite": kwrite, "kget": kget, "kfind": lambda *a, **k: [], "call": lambda *a, **k: (None, None)}, writes

WF = [{"step": "verify", "role": "verifier", "task": "Verify docs"},
      {"step": "issue", "role": "issuer", "task": "Issue cert", "issues": "birth_certificate"},
      {"step": "deliver", "role": "clerk", "task": "Hand over"}]
OFF = {"title": "Birth Cert", "fulfillment": {"workflow": WF}}
CASE = {"id": "ds:prov:case/svcA-42", "tenant": "prov", "service": "svcA",
        "opened_by": "user://prov/citizen9", "scope": {"ward": "12"}, "current_step": "verify"}

ok = True
def check(name, cond):
    global ok
    print(("  PASS " if cond else "  FAIL ") + name); ok = ok and cond

# 1) multi-step advance: complete 'verify' → next task+inbox for 'issue', case advances, txn in_progress
ctx, W = make_ctx({"task": {"workflow_step": "verify"}, "case": CASE, "offering": OFF})
res, err = lce.h_service_advance(ctx)
check("1 no error", err is None)
check("1 advanced_to=issue", res.get("advanced_to") == "issue")
check("1 routed_to=issuer", res.get("routed_to") == "issuer")
check("1 txn in_progress", res.get("txn") == {"status": "in_progress", "step": "issue"})
check("1 no issue instr (verify doesn't issue)", "issue" not in res)
check("1 next task written", W.get("ds:prov:task/svcA-42-issue", {}).get("assignee") == "role://prov/issuer")
check("1 next inbox written", W.get("ds:prov:platform/inbox_item/svcA-42-issue", {}).get("actor") == "role://prov/issuer")
check("1 case current_step=issue", W.get("ds:prov:case/svcA-42", {}).get("current_step") == "issue")

# 2) issues-step: complete 'issue' → VC instruction with citizen submission claims + advance to 'deliver'
ctx, W = make_ctx({"task": {"workflow_step": "issue"}, "case": CASE, "offering": OFF},
                  subm={"data": {"name": "Baby X", "dob": "2026-01-01"}})
res, err = lce.h_service_advance(ctx)
check("2 advanced_to=deliver", res.get("advanced_to") == "deliver")
check("2 issue instruction present", isinstance(res.get("issue"), dict))
check("2 issue type", res.get("issue", {}).get("type") == "birth_certificate")
check("2 issue holder=citizen", res.get("issue", {}).get("holder") == "user://prov/citizen9")
check("2 issue claims from submission", res.get("issue", {}).get("claims") == {"name": "Baby X", "dob": "2026-01-01"})

# 3) final step: complete 'deliver' → close case, txn resolved, no next task
ctx, W = make_ctx({"task": {"workflow_step": "deliver"}, "case": CASE, "offering": OFF})
res, err = lce.h_service_advance(ctx)
check("3 case_closed", res.get("case_closed") is True)
check("3 advanced_to=resolved", res.get("advanced_to") == "resolved")
check("3 txn resolved", res.get("txn") == {"status": "resolved", "step": "resolved"})
check("3 case row closed", W.get("ds:prov:case/svcA-42", {}).get("status") == "closed")

# 4) no workflow (single-step service) → {no_workflow:true}, host flat-closes
ctx, W = make_ctx({"task": {"workflow_step": "x"}, "case": CASE, "offering": {"fulfillment": {}}})
res, err = lce.h_service_advance(ctx)
check("4 no_workflow", res.get("no_workflow") is True and not W)

# === service-open (case OPENING interpreter, ported from _provider_fulfil) ===
OFF_OPEN = {"id": "ds:prov:service.offering/svcA", "tenant": "prov", "service_id": "svcA", "title": "Birth Cert",
            "solution": "ds:prov:solution/svcA",
            "fulfillment": {"workflow": [{"step": "triage", "role": "verifier", "task": "Verify docs"},
                                         {"step": "issue", "role": "issuer", "task": "Issue"}]}}
MSG = {"form": {"name": "Baby X", "ward": "12"}, "description": "New birth certificate application",
       "_verified_credentials": ["ds:prov:vc/hospital-1"]}
ctx, _ = make_ctx({"offering": OFF_OPEN, "message": MSG, "citizen_sub": "user://prov/citizen9",
                   "scope": {"area": "ward/12", "_path": ["ward/12"]}})
res, err = lce.h_service_open(ctx)
W = {w["id"]: w["object"] for w in (res.get("writes") or [])}
check("O no error", err is None)
check("O returns write-set (case+submission+task+inbox = 4)", len(res.get("writes") or []) == 4)
check("O case reference", res.get("reference") == "SVCA-" + res["case"].rsplit("-", 1)[-1])
check("O routed_to=verifier (first step)", res.get("routed_to") == "verifier")
check("O fulfilled_by=solution", res.get("fulfilled_by") == "ds:prov:solution/svcA")
cid = res["case"]
case = W.get(cid, {})
check("O case owned by provider", case.get("tenant") == "prov")
check("O case opened_by citizen", case.get("opened_by") == "user://prov/citizen9")
check("O case carries scope", case.get("scope") == {"area": "ward/12", "_path": ["ward/12"]})
check("O case workflow=solution", case.get("workflow") == "ds:prov:solution/svcA")
check("O case subjects incl offering + VC", case.get("subjects") == ["ds:prov:service.offering/svcA", "ds:prov:vc/hospital-1"])
subm = next((o for o in W.values() if o.get("schema") == "civic.submission"), {})
check("O submission persisted with form data", subm.get("data") == {"name": "Baby X", "ward": "12"})
task = next((o for o in W.values() if o.get("schema") == "platform.task"), {})
check("O first task to verifier role", task.get("assignee") == "role://prov/verifier")
check("O first task step=triage", task.get("workflow_step") == "triage")
inbox = next((o for o in W.values() if o.get("schema") == "platform.inbox_item"), {})
check("O inbox to verifier role", inbox.get("actor") == "role://prov/verifier")

# O2: no-form, single-step assign_role shorthand → case + task + inbox (no submission)
ctx, _ = make_ctx({"offering": {"id": "o2", "tenant": "prov", "service_id": "svcB", "solution": "s",
                                "fulfillment": {"assign_role": "clerk", "first_task": "Handle"}},
                   "message": {}, "citizen_sub": "user://prov/c", "scope": None})
res2, err2 = lce.h_service_open(ctx)
check("O2 no submission (no form)", not any(o.get("schema") == "civic.submission" for o in [w["object"] for w in res2["writes"]]))
check("O2 routed via assign_role shorthand", res2.get("routed_to") == "clerk")

print("\nRESULT:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
