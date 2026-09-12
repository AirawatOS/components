#!/usr/bin/env python3
"""news-classify engine — classify governed news.item → civic.issue, then route ACTIONABLE issues to the
responsible agency on the decision→task→Inbox spine (News vertical, slice 2).

For each unclassified news.item it asks the model gateway (/v1/complete) to assign a civic category (incl.
law_and_order), a severity, and whether it is actionable by a city agency. It writes a civic.issue per item.
For actionable issues (severity in ROUTE_SEVERITIES, or category in ROUTE_CATEGORIES) it opens the ADR-0029
loop: a governance.recommendation + platform.case + a decision platform.task (assignee = the mapped agency
role) + a platform.inbox_item — so a human at that agency admits/acts. The AI never decides; it proposes.

All I/O is the broker under the run-token. NO synthetic data — it classifies only real news.item that exist;
if the model gateway is unavailable it fails loudly rather than inventing categories.

  Env: TENANT/WRITE_TENANT · RUN_TOKEN · MAX (items/run, default 60) · BATCH (per LLM call, default 8)
       AGENCY_MAP (JSON category→role) · ROUTE_SEVERITIES (csv, default "high,critical")
       ROUTE_CATEGORIES (csv, default "law_and_order,disaster")
"""
import hashlib, json, os, re, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT", "")
MAX = int(os.environ.get("MAX", "60"))
BATCH = int(os.environ.get("BATCH", "8"))
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
METHOD = "news-classify@0.1.0"
CATEGORIES = ["law_and_order", "water", "sanitation", "health", "roads_transport", "environment",
              "infrastructure", "governance", "disaster", "welfare", "education", "other"]
ROUTE_SEV = set((os.environ.get("ROUTE_SEVERITIES", "high,critical")).split(","))
ROUTE_CAT = set((os.environ.get("ROUTE_CATEGORIES", "law_and_order,disaster")).split(","))
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


def _call(path, body, timeout=90):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def query(scope):
    return [r.get("object", r) for r in _call("/v1/query", {"scope": scope}).get("results", [])]


def write(scope, oid, obj):
    obj = {k: v for k, v in obj.items() if v is not None}
    _call("/v1/write", {"scope": scope, "id": oid, "object": obj, "valid_time": {"from": NOW}, "asserted_at": NOW})


def _json_array(text):
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except Exception:
        return []


def classify_batch(items):
    """One /v1/complete call classifying a batch of news items → list of {category,severity,actionable,rationale}."""
    lines = "\n".join(f"{i}. {(it.get('title') or '')} — {(it.get('summary') or '')[:200]}"
                      for i, it in enumerate(items))
    prompt = (
        "You are a municipal operations analyst. Classify each news item below into a CIVIC ISSUE.\n"
        f"Allowed categories: {', '.join(CATEGORIES)}.\n"
        "For EACH numbered item return an object with: index (int), category (one allowed value), "
        "severity (low|medium|high|critical — how urgently a city agency must act), "
        "actionable (true only if a CITY AGENCY can/should act on it; national politics, sports, etc. = false), "
        "one_line (a <=15-word action-oriented summary).\n"
        "Return ONLY a JSON array, no prose.\n\nITEMS:\n" + lines)
    try:
        res = _call("/v1/complete", {"messages": [{"role": "user", "content": prompt}], "max_tokens": 2000})
    except Exception as e:
        raise SystemExit(f"FATAL: /v1/complete unavailable ({e}) — cannot classify (no synthetic fallback).")
    out = {}
    for r in _json_array(res.get("content", "")):
        if isinstance(r, dict) and "index" in r:
            out[int(r["index"])] = r
    return out


def route(issue, role):
    """Open the ADR-0029 decision loop for an actionable issue, routed to the agency role."""
    key = issue["id"].rsplit("/", 1)[-1]
    assignee = f"role://{TENANT}/{role}"
    case_id = f"ds:{TENANT}:case/civic-{key}"
    rec_id = f"ds:{TENANT}:governance/recommendation/civic-{key}"
    task_id = f"ds:{TENANT}:task/civic-{key}"
    iid = f"ds:{TENANT}:platform/inbox_item/civic-{key}"
    write("platform.case", case_id, {"schema": "platform.case", "id": case_id, "tenant": TENANT,
          "title": f"Civic issue: {issue['title'][:80]}", "status": "open", "opened_at": NOW})
    write("governance.recommendation", rec_id, {"schema": "governance.recommendation", "id": rec_id, "tenant": TENANT,
          "case": case_id, "for_task": task_id, "title": f"Route to {role}: {issue['title'][:80]}",
          "summary": issue.get("summary", ""), "proposed_action": f"Assign to {role} for action.",
          "produced_by": "ds:airawat:engine/news-classify",
          "confidence": issue.get("confidence"), "evidence": [issue.get("source_ref")], "created_at": NOW})
    write("platform.task", task_id, {"schema": "platform.task", "id": task_id, "tenant": TENANT, "case": case_id,
          "verb": "decide", "title": f"[{issue['category']}/{issue.get('severity')}] {issue['title'][:90]}",
          "assignee": assignee, "actions": ["approve", "reject"],
          "status": "open", "created_at": NOW})
    write("platform.inbox_item", iid, {"schema": "platform.inbox_item", "id": iid, "tenant": TENANT,
          "actor": assignee, "title": f"Decide: {issue['title'][:80]}", "ref": task_id, "kind": "task",
          "actions": ["approve", "reject"], "status": "unread", "created_at": NOW})
    return task_id


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — refusing to guess.")
    done = set()
    if os.environ.get("RECLASSIFY", "0") != "1":                # RECLASSIFY=1 → re-run over all news (upserts by id)
        for o in query("civic.issue"):
            if o.get("source_ref"):
                done.add(o["source_ref"])
    news = [o for o in query("news.item") if o.get("id") and o["id"] not in done]
    news.sort(key=lambda o: o.get("published_at") or o.get("observed_at") or "", reverse=True)
    news = news[:MAX]
    if not news:
        print("[news-classify] no unclassified news.item — nothing to do"); print('RESULT: {"classified": 0, "routed": 0}'); return 0
    classified = routed = 0
    for b in range(0, len(news), BATCH):
        batch = news[b:b + BATCH]
        verdicts = classify_batch(batch)
        for i, it in enumerate(batch):
            v = verdicts.get(i) or {}
            cat = v.get("category") if v.get("category") in CATEGORIES else "other"
            sev = (v.get("severity") or "low").lower()
            role = AGENCY_MAP.get(cat, AGENCY_MAP["other"])
            actionable = bool(v.get("actionable")) or cat in ROUTE_CAT
            iid = f"ds:{TENANT}:civic/issue/{hashlib.sha1(it['id'].encode()).hexdigest()[:16]}"
            issue = {"schema": "civic.issue", "id": iid, "tenant": TENANT,
                     "title": (v.get("one_line") or it.get("title") or "")[:200],
                     "summary": (it.get("summary") or it.get("title") or "")[:600],
                     "category": cat, "jurisdiction": it.get("jurisdiction") or TENANT, "severity": sev,
                     "agency": role, "source_ref": it["id"], "source_url": it.get("url"),
                     "status": "open", "confidence": 0.7, "method": METHOD, "observed_at": NOW}
            do_route = actionable and (sev in ROUTE_SEV or cat in ROUTE_CAT)
            if do_route:
                issue["status"] = "routed"
            write("civic.issue", iid, issue)
            classified += 1
            if do_route:
                route(issue, role); routed += 1
        print(f"  classified {min(b+BATCH,len(news))}/{len(news)} · routed {routed}", flush=True)
    print(f"[news-classify] classified {classified} civic.issue · routed {routed} to agencies", flush=True)
    print("RESULT: " + json.dumps({"classified": classified, "routed": routed}))


if __name__ == "__main__":
    main()
