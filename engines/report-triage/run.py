#!/usr/bin/env python3
"""report-triage engine — human-REPORTED incidents (signal.incident) → civic.issue → routed to the responsible
agency on the decision→task→Inbox spine (Civic vertical, slice 3).

The intake sibling of news-classify: instead of news, the source is a person's report (ground team / certified
volunteer / citizen). Trust is tiered:
  - ground_team                          → AUTHORITATIVE (always routed; confidence 0.85)
  - certified_volunteer WITH a valid VC  → AUTHORITATIVE (always routed; confidence 0.9) — credential.volunteer
      verified: holder == reporter, status active, scope matches the jurisdiction
  - certified_volunteer WITHOUT a valid VC, or citizen/anonymous → routed only if severity high/critical OR
      category law_and_order/disaster; otherwise the civic.issue is left status=open (a verification queue).
The reporter supplies category + severity; triage trusts them (no LLM needed) and maps category → agency role.
ADR-0029: the engine PROPOSES the issue + a recommendation; a human at the agency ADMITS. All I/O via the broker.

  Env: TENANT/WRITE_TENANT · RUN_TOKEN · MAX (default 100) · AGENCY_MAP (JSON category→role)
       CITIZEN_ROUTE_SEVERITIES (csv, default "high,critical") · CITIZEN_ROUTE_CATEGORIES (csv, default "law_and_order,disaster")
"""
import hashlib, json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT", "")
MAX = int(os.environ.get("MAX", "100"))
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
METHOD = "report-triage@0.1.0"
CATEGORIES = ["law_and_order", "water", "sanitation", "health", "roads_transport", "environment",
              "infrastructure", "governance", "disaster", "welfare", "education", "other"]
CIT_SEV = set((os.environ.get("CITIZEN_ROUTE_SEVERITIES", "high,critical")).split(","))
CIT_CAT = set((os.environ.get("CITIZEN_ROUTE_CATEGORIES", "law_and_order,disaster")).split(","))
_DEFAULT_MAP = {
    "law_and_order": "safety.ground-marshal", "disaster": "safety.ground-marshal", "safety": "safety.ground-marshal",
    "health": "health.medical-responder", "sanitation": "urban.sanitation-officer",
    "environment": "urban.sanitation-officer", "water": "urban.ward-officer", "roads_transport": "msrtc-scheduler",
    "infrastructure": "urban.ward-officer", "governance": "urban.ward-officer", "welfare": "civic.grievance-officer",
    "education": "urban.ward-officer", "other": "civic.grievance-officer",
}
try:
    AGENCY_MAP = {**_DEFAULT_MAP, **json.loads(os.environ.get("AGENCY_MAP", "") or "{}")}
except Exception:
    AGENCY_MAP = _DEFAULT_MAP


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def query(scope):
    return [r.get("object", r) for r in _call("/v1/query", {"scope": scope}).get("results", [])]


def write(scope, oid, obj):
    obj = {k: v for k, v in obj.items() if v is not None}
    _call("/v1/write", {"scope": scope, "id": oid, "object": obj, "valid_time": {"from": NOW}, "asserted_at": NOW})


def route(issue, role, note):
    key = issue["id"].rsplit("/", 1)[-1]
    assignee = f"role://{TENANT}/{role}"
    case_id = f"ds:{TENANT}:case/{key}"
    rec_id = f"ds:{TENANT}:governance/recommendation/{key}"
    task_id = f"ds:{TENANT}:task/{key}"
    iid = f"ds:{TENANT}:platform/inbox_item/{key}"
    write("platform.case", case_id, {"schema": "platform.case", "id": case_id, "tenant": TENANT,
          "title": f"Civic issue: {issue['title'][:80]}", "status": "open", "opened_at": NOW})
    write("governance.recommendation", rec_id, {"schema": "governance.recommendation", "id": rec_id, "tenant": TENANT,
          "case": case_id, "for_task": task_id, "title": f"Route to {role}: {issue['title'][:80]}",
          "summary": issue.get("summary", ""), "proposed_action": f"Assign to {role} for action. ({note})",
          "produced_by": "ds:airawat:engine/report-triage", "confidence": issue.get("confidence"),
          "evidence": [issue.get("source_ref")], "created_at": NOW})
    write("platform.task", task_id, {"schema": "platform.task", "id": task_id, "tenant": TENANT, "case": case_id,
          "verb": "decide", "title": f"[{issue['category']}/{issue.get('severity')}] {issue['title'][:88]} · {note}",
          "assignee": assignee, "actions": ["approve", "reject"], "status": "open", "created_at": NOW})
    write("platform.inbox_item", iid, {"schema": "platform.inbox_item", "id": iid, "tenant": TENANT,
          "actor": assignee, "title": f"Decide: {issue['title'][:80]}", "ref": task_id, "kind": "task",
          "actions": ["approve", "reject"], "status": "unread", "created_at": NOW})


def valid_volunteer_cred(reporter, cred_ref, juris, creds):
    for c in creds:
        if c.get("status") != "active":
            continue
        if (cred_ref and c.get("id") == cred_ref) or (reporter and c.get("holder") == reporter):
            scope = c.get("scope")
            if not scope or scope == juris:
                return c
    return None


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — refusing to guess.")
    done = {o.get("source_ref") for o in query("civic.issue") if o.get("source_ref")}
    creds = query("credential.volunteer")
    incidents = [o for o in query("signal.incident") if o.get("id") and o["id"] not in done]
    incidents.sort(key=lambda o: o.get("observed_at") or "", reverse=True)
    incidents = incidents[:MAX]
    if not incidents:
        print("[report-triage] no untriaged signal.incident — nothing to do"); print('RESULT: {"triaged": 0, "routed": 0}'); return 0
    triaged = routed = 0
    for it in incidents:
        rk = (it.get("reporter_kind") or "citizen").lower()
        juris = it.get("jurisdiction") or TENANT
        cat = it.get("category") if it.get("category") in CATEGORIES else "other"
        sev = (it.get("severity") or "medium").lower()
        role = AGENCY_MAP.get(cat, AGENCY_MAP["other"])
        # trust tiering
        verified = False; note = "citizen report (unverified)"
        if rk == "certified_volunteer":
            verified = bool(valid_volunteer_cred(it.get("reporter"), it.get("credential_ref"), juris, creds))
            note = "✓ certified volunteer (VC-verified)" if verified else "certified_volunteer — VC NOT verified"
        elif rk == "ground_team":
            note = "ground team"
        authoritative = (rk == "ground_team") or (rk == "certified_volunteer" and verified)
        conf = 0.9 if (rk == "certified_volunteer" and verified) else (0.85 if rk == "ground_team" else 0.5)
        do_route = authoritative or (sev in CIT_SEV) or (cat in CIT_CAT)
        iid = f"ds:{TENANT}:civic/issue/inc-{hashlib.sha1(it['id'].encode()).hexdigest()[:14]}"
        issue = {"schema": "civic.issue", "id": iid, "tenant": TENANT,
                 "title": (it.get("title") or "")[:200],
                 "summary": (it.get("description") or it.get("title") or "")[:600] + f"  [reported: {note}]",
                 "category": cat, "jurisdiction": juris, "severity": sev, "agency": role,
                 "source_ref": it["id"], "source_url": (it.get("media") or [None])[0],
                 "status": "routed" if do_route else "open", "confidence": conf, "method": METHOD, "observed_at": NOW}
        write("civic.issue", iid, issue); triaged += 1
        if do_route:
            route(issue, role, note); routed += 1
        print(f"  {it['id'].rsplit('/',1)[-1]}: {rk} → {cat}/{sev} → {'ROUTED '+role if do_route else 'held (verify)'} [{note}]", flush=True)
    print(f"[report-triage] triaged {triaged} incidents · routed {routed} to agencies", flush=True)
    print("RESULT: " + json.dumps({"triaged": triaged, "routed": routed}))


if __name__ == "__main__":
    main()
