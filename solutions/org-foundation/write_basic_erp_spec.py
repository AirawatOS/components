"""ADR-0109: write the governed platform.service_spec instance for Basic ERP (Org Foundation).
Reverse-authored from the running org/hr/finance/project/payroll engines. Run inside app-host as steward/airawat.
The platform.service_spec schema is already governed; this writes the INSTANCE (attributed steward write)."""
import json, os, urllib.request, urllib.error
IDN=os.environ.get("IDN","http://identity:8093"); KERNEL=os.environ.get("KERNEL","http://kernel:8080")
SPEC_ID="ds:airawat:service_spec/basic-erp"
def post(u,b,t=None):
    h={"Content-Type":"application/json"}
    if t:h["Authorization"]="Bearer "+t
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(u,data=json.dumps(b).encode(),headers=h,method="POST"),timeout=25).read() or b"{}"), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code} {e.read().decode()[:200]}"
TOK=post(IDN+"/realms/airawat/login",{"username":"steward","password":os.environ.get("AIRAWAT_PASSWORD") or "Airawat@123"})[0]["access_token"]

spec={
 "domain_model":[
  {"noun":"org.org_unit","status":"built","role":"department / team node","impl":"org-rbac-engine"},
  {"noun":"org.position","status":"built","role":"a job slot (level+stream)","impl":"org-rbac-engine"},
  {"noun":"org.posting","status":"built","role":"person→position assignment (→RBAC)","impl":"org-rbac-engine (posting-apply)"},
  {"noun":"org.employee","status":"built","role":"a person on staff; .citizen links the login","impl":"org-rbac-engine (employee-provision)"},
  {"noun":"governance.role_grant","status":"built","role":"effective role a posting projects","impl":"org-rbac-engine"},
  {"noun":"hr.job_level","status":"built","role":"grade/band","impl":"policy-engine"},
  {"noun":"org.policy","status":"built","role":"resolvable HR/finance policy","impl":"policy-engine (policy-resolve)"},
  {"noun":"hr.leave_request","status":"built","role":"leave application + approval state","impl":"hr-workflows (h_leave_apply)"},
  {"noun":"hr.attendance","status":"built","role":"check-in/out record","impl":"hr-workflows (attendance-mark)"},
  {"noun":"hr.expense_claim","status":"built","role":"expense claim + settlement","impl":"hr-workflows (h_expense_apply)"},
  {"noun":"hr.compensation","status":"built","role":"pay basis (drives payroll)","impl":"payroll-engine"},
  {"noun":"finance.account","status":"built","role":"chart-of-accounts node","impl":"finance-engine"},
  {"noun":"finance.budget","status":"built","role":"budget envelope","impl":"finance-engine"},
  {"noun":"finance.transaction","status":"built","role":"double-entry posting","impl":"finance-engine (finance-post)"},
  {"noun":"project.proposal","status":"built","role":"proposed project awaiting approval","impl":"project-engine (proposal-submit)"},
  {"noun":"project.project","status":"built","role":"approved project under execution","impl":"project-engine"},
  {"noun":"project.allocation","status":"built","role":"time/cost allocation to a project","impl":"project-engine"},
  {"noun":"project.review","status":"built","role":"periodic project review","impl":"project-engine"},
  {"noun":"project.evaluation","status":"built","role":"end-of-project evaluation","impl":"project-engine"},
  {"noun":"platform.inbox_item","status":"built","role":"routed approval task","impl":"foundation"},
  {"noun":"platform.schedule","status":"built","role":"recurring payroll schedule","impl":"payroll-engine"},
 ],
 "operations":[
  {"verb":"employee-provision","status":"built","role":"create employee + link login","impl":"org-rbac-engine (h_employee_provision)"},
  {"verb":"posting-apply","status":"built","role":"assign posting → role_grant","impl":"org-rbac-engine (h_posting_apply)"},
  {"verb":"authz-check","status":"built","role":"resolve effective grants","impl":"org-rbac-engine (h_authz_check)"},
  {"verb":"policy-resolve","status":"built","role":"resolve applicable policy","impl":"policy-engine (h_policy_resolve)"},
  {"verb":"leave-apply","status":"built","role":"file leave → route to manager","impl":"hr-workflows (h_leave_apply)"},
  {"verb":"leave-decide","status":"built","role":"manager approves/rejects leave","impl":"hr-workflows (h_leave_decide)"},
  {"verb":"attendance-mark","status":"built","role":"check in / out","impl":"hr-workflows (attendance-mark)"},
  {"verb":"expense-apply","status":"built","role":"file expense claim (policy-limited)","impl":"hr-workflows (h_expense_apply)"},
  {"verb":"expense-decide","status":"built","role":"approve/reject → settle to finance","impl":"hr-workflows (h_expense_decide)"},
  {"verb":"finance-post","status":"built","role":"post double-entry txn, commit budget","impl":"finance-engine (h_finance_post)"},
  {"verb":"proposal-submit","status":"built","role":"submit project proposal","impl":"project-engine (h_proposal_submit)"},
  {"verb":"proposal-decide","status":"built","role":"approve/reject proposal → open project","impl":"project-engine (h_proposal_decide)"},
  {"verb":"payroll-apportion","status":"built","role":"apportion pay across allocations","impl":"payroll-engine (h_payroll_apportion)"},
  {"verb":"payroll-run","status":"built","role":"run payroll → finance transactions","impl":"payroll-engine (h_payroll_run)"},
 ],
 "business_rules":[
  {"id":"ERP-EMP-LOGIN","status":"built","statement":"a person can act in HR Desk only if an org.employee links their login (.citizen==sub)","impl":"hr-workflows (_me); UI disables Apply if unlinked"},
  {"id":"ERP-LEAVE-ROUTE","status":"built","statement":"a filed leave request routes to employee.manager as an inbox task; no manager → filed but unrouted","impl":"hr-workflows (h_leave_apply)"},
  {"id":"ERP-LEAVE-DECIDE","status":"built","statement":"only the named manager_login may approve/reject a leave request","impl":"hr-workflows (h_leave_decide)"},
  {"id":"ERP-LEAVE-BALANCE","status":"partial","statement":"requested days must not exceed policy-resolved balance (unlimited if no leave policy)","impl":"hr-workflows (_leave_balance)"},
  {"id":"ERP-EXPENSE-LIMIT","status":"built","statement":"an expense above the policy limit needs approval before it settles to finance","impl":"hr-workflows + policy-engine + finance-engine"},
  {"id":"ERP-POSTING-RBAC","status":"built","statement":"applying a posting projects a governance.role_grant so the person gains the role live","impl":"org-rbac-engine (h_posting_apply)"},
  {"id":"ERP-FINANCE-DE","status":"built","statement":"every finance.transaction is balanced double-entry and commits against a budget","impl":"finance-engine (h_finance_post)"},
  {"id":"ERP-PROJ-GATE","status":"built","statement":"a proposal becomes a project only on an authorised approval","impl":"project-engine (h_proposal_decide)"},
  {"id":"ERP-PAYROLL-APPORT","status":"built","statement":"payroll apportions pay across project.allocations (day-fraction for partial periods)","impl":"payroll-engine (h_payroll_apportion)"},
 ],
 "workflow":{
  "hr_leave":{"status":"built","states":["draft","pending","approved","rejected"],"impl":"hr-workflows (platform.lifecycle hr.leave)"},
  "hr_expense":{"status":"built","states":["filed","policy_checked","approved","settled","rejected"],"impl":"hr-workflows + finance-engine"},
  "project":{"status":"built","states":["proposal","approved","executing","reviewed","evaluated","adjusted"],"impl":"project-engine"},
  "payroll":{"status":"built","states":["preview","apportioned","posted"],"impl":"payroll-engine"},
  "onboarding":{"status":"partial","states":["employee_created","login_linked","posted","role_granted"],"impl":"org-rbac-engine"},
 },
 "roles":[
  {"role":"employee","status":"built","role_desc":"files own leave/expense/attendance","impl":"hr-desk (roles: [])"},
  {"role":"manager","status":"built","role_desc":"approves team leave/expense","impl":"resolved from org.employee.manager"},
  {"role":"hr-admin","status":"partial","role_desc":"sets up employees, postings, policy","impl":"org-setup + org-rbac-engine"},
  {"role":"finance-officer","status":"partial","role_desc":"posts transactions, manages budgets","impl":"finance-console + finance-engine"},
 ],
 "acceptance_criteria":[
  {"id":"AC-EMP-LINK","status":"built","test":None,"traces":["ERP-EMP-LOGIN"],"given":"a login with no linked employee","when":"it tries leave-apply","then":"refused with a clear 'link your login' message; Apply button disabled"},
  {"id":"AC-LEAVE-ROUTE","status":"built","test":None,"traces":["ERP-LEAVE-ROUTE"],"given":"an employee with a manager","when":"they leave-apply","then":"hr.leave_request(pending) written AND inbox task routed to the manager"},
  {"id":"AC-LEAVE-DECIDE","status":"built","test":None,"traces":["ERP-LEAVE-DECIDE"],"given":"a pending leave request","when":"the named manager approves","then":"it becomes approved, stamped with the decider"},
  {"id":"AC-EXPENSE-LIMIT","status":"built","test":None,"traces":["ERP-EXPENSE-LIMIT"],"given":"an expense above the policy limit","when":"filed and approved","then":"a balanced finance.transaction settles it against the budget"},
  {"id":"AC-POSTING-RBAC","status":"built","test":None,"traces":["ERP-POSTING-RBAC"],"given":"a person and a position","when":"posting-apply runs","then":"a role_grant appears so authz-check returns the new role"},
  {"id":"AC-PAYROLL-APPORT","status":"built","test":None,"traces":["ERP-PAYROLL-APPORT"],"given":"employees with compensation + allocations","when":"payroll runs","then":"finance.transactions apportioned across each allocation"},
 ],
 "nfr":[
  {"id":"NFR-ATTRIBUTED","status":"built","statement":"every write attributed to the acting engine obo the user (ADR-0052)"},
  {"id":"NFR-INBOX","status":"built","statement":"approvals surface + are actionable in the universal Inbox (ADR-0048)"},
  {"id":"NFR-SETUP","status":"partial","statement":"a tenant is usable only after employees exist AND each login is linked AND managers are set (the nashik gap)"},
 ],
}
# NB: platform.service_spec is additionalProperties:false — allowed top-level keys are
# capability,id,realized_by,schema,status,version,spec,source_yaml (NO constitution/tenant).
# The constitution reference lives inside spec{}; the generate branch defaults to
# ds:airawat:constitution/default when the envelope has none.
spec["constitution"]="ds:airawat:constitution/default"
obj={"schema":"platform.service_spec","id":SPEC_ID,
     "capability":"Run an organisation's backbone: structure & people (org units, positions, postings→live RBAC), HR (leave, attendance, expense), finance (double-entry, budgets, transactions), projects (propose→approve→execute→review→evaluate), and payroll apportioned to project work — governed records with manager-routed approvals in a universal Inbox.",
     "version":"0.1.0","realized_by":"ds:airawat:solution/org-foundation",
     "status":"partial","spec":spec,"source_yaml":"components/solutions/org-foundation/basic-erp.service-spec.yaml"}
_,we=post(KERNEL+"/v1/registry/objects",{"id":SPEC_ID,"object":obj},TOK)
print("write basic-erp service_spec:","OK" if not we else "ERR "+str(we))
# verify
r,_=post(KERNEL+"/v1/registry/resolve",{"id":SPEC_ID},TOK)
o=((r.get("results") or [{}])[0].get("object") or {}) if r else {}
sp=o.get("spec") or {}
print("  capability:", (o.get("capability") or "")[:60])
print("  targets: operations=%d nouns=%d workflows=%d | acceptance=%d rules=%d" % (
  len(sp.get("operations",[])), len(sp.get("domain_model",[])), len(sp.get("workflow",{})),
  len(sp.get("acceptance_criteria",[])), len(sp.get("business_rules",[]))))
