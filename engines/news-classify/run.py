#!/usr/bin/env python3
"""news-classify engine (v0.2.0) — LLM categorization of governed news.item.

Reads the tenant's news.taxonomy (categories + agencies of interest — admin-configurable; a built-in Delhi
default is seeded if none exists). For each news.item it asks the model gateway to return, PER ITEM: civic
topics (from the tenant's categories), the responsible AGENCY (from the tenant's agencies, e.g. Delhi Jal
Board / Electricity / Gas / Municipal), whether the article is actually about the item's place
(place_relevant — so national/foreign news fetched by a place query drops out of the local view), a civic
category + severity + actionability.

It then (1) WRITES the accurate topics + agency + place_relevant back onto the news.item — replacing the
ingest driver's naive substring tags, so the News reader filters correctly; and (2) for ACTIONABLE items opens
the ADR-0029 loop (civic.issue → recommendation → case → decision task → inbox) routed to the mapped agency.

LLM-only — NO substring matching, NO synthetic data. Runs TWO ways over injected I/O: confined over the broker
(main(), X-Run-Token) and host-dispatched with ctx.ai_complete (HANDLERS['news-reclassify']) — the latter is
how the LLM is reached where the confined /v1/complete route isn't wired (the model gateway is host-held).

  Env: TENANT/WRITE_TENANT · RUN_TOKEN · MAX (items/run, 120) · BATCH (per LLM call, 8) · RECLASSIFY (1=all)
       AGENCY_MAP (JSON category→role) · ROUTE_SEVERITIES (csv) · ROUTE_CATEGORIES (csv)
"""
import hashlib, json, os, re, threading, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
METHOD = "news-classify-llm@0.2.0"
CATEGORIES = ["law_and_order", "water", "sanitation", "health", "roads_transport", "environment",
              "infrastructure", "governance", "disaster", "welfare", "education", "other"]
_DEFAULT_MAP = {
    "law_and_order": "safety.ground-marshal", "disaster": "safety.ground-marshal", "safety": "safety.ground-marshal",
    "health": "health.medical-responder", "sanitation": "urban.sanitation-officer",
    "environment": "urban.sanitation-officer", "water": "urban.ward-officer", "roads_transport": "msrtc-scheduler",
    "infrastructure": "urban.ward-officer", "governance": "urban.ward-officer", "welfare": "civic.grievance-officer",
    "education": "urban.ward-officer", "other": "civic.grievance-officer",
}
DEFAULT_TOPICS = ["water", "health", "transport", "safety", "governance", "environment",
                  "infrastructure", "education", "welfare"]
DEFAULT_AGENCIES = [
    {"key": "jal-board", "label": "Water Board (Delhi Jal Board)",
     "hint": "water supply, sewerage, pipelines, tankers, water shortage/leakage/contamination, STPs"},
    {"key": "electricity", "label": "Electricity (BSES/Tata/DERC)", "hint": "power supply, outages, tariffs, transformers, discoms"},
    {"key": "gas", "label": "Gas (IGL)", "hint": "piped natural gas, CNG, gas leaks/supply"},
    {"key": "municipal", "label": "Municipal Corporation (MCD/NDMC)",
     "hint": "sanitation, garbage, roads, buildings, licensing, property tax, encroachment"},
    {"key": "dda", "label": "Development Authority (DDA)", "hint": "land, housing, development, master plan"},
    {"key": "police", "label": "Police", "hint": "law and order, crime, traffic enforcement"},
    {"key": "pwd", "label": "Public Works (PWD)", "hint": "roads, flyovers, storm-water drains, public works"},
    {"key": "transport", "label": "Transport (DTC/DMRC)", "hint": "city buses, metro, permits, fares"},
    {"key": "pollution", "label": "Pollution Control (DPCC/CAQM)", "hint": "air/water pollution, environment compliance"},
    {"key": "none", "label": "Other / None", "hint": "not specific to a single city agency"},
]


def _call(path, body, timeout=90):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": os.environ.get("RUN_TOKEN", "")}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


# I/O is INJECTED so the SAME logic runs confined over the broker (main()) and host-dispatched with
# ctx.ai_complete / ctx.kfind / ctx.kwrite (HANDLERS). The host path reaches the (host-held) model gateway.
class _IO:
    def __init__(self, qfn, wfn, aifn): self.query, self.write, self.ai = qfn, wfn, aifn


def _broker_io():
    def q(scope): return [r.get("object", r) for r in _call("/v1/query", {"scope": scope}).get("results", [])]
    def w(scope, oid, obj):
        obj = {k: v for k, v in obj.items() if v is not None}
        _call("/v1/write", {"scope": scope, "id": oid, "object": obj, "valid_time": {"from": NOW}, "asserted_at": NOW})
    def ai(m, mt): return _call("/v1/complete", {"messages": m, "max_tokens": mt}).get("content", "")
    return _IO(q, w, ai)


def _ctx_io(ctx):
    def q(scope): return ctx["kfind"](scope)
    def w(scope, oid, obj): ctx["kwrite"](oid, {k: v for k, v in obj.items() if v is not None})
    def ai(m, mt): return ctx["ai_complete"](m, mt)
    return _IO(q, w, ai)


def _json_array(text):
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except Exception:
        return []


def load_taxonomy(io, tenant):
    tid = f"ds:{tenant}:news/taxonomy"
    for o in io.query("news.taxonomy"):
        if o.get("id") == tid or o.get("tenant") == tenant:
            return (o.get("categories") or DEFAULT_TOPICS), (o.get("agencies") or DEFAULT_AGENCIES)
    rec = {"schema": "news.taxonomy", "id": tid, "tenant": tenant, "categories": DEFAULT_TOPICS,
           "agencies": DEFAULT_AGENCIES, "updated_at": NOW, "updated_by": METHOD}
    try:
        io.write("news.taxonomy", tid, rec); print(f"[news-classify] seeded default news.taxonomy for {tenant}")
    except Exception as e:
        print(f"[news-classify] WARN could not seed news.taxonomy ({e})")
    return DEFAULT_TOPICS, DEFAULT_AGENCIES


def classify_batch(io, items, topics_vocab, agencies, tenant):
    lines = "\n".join(f"{i}. [{(it.get('jurisdiction') or tenant)}] {(it.get('title') or '')} — {(it.get('summary') or '')[:200]}"
                      for i, it in enumerate(items))
    ag_lines = "\n".join(f"  - {a['key']}: {a.get('label','')} — {a.get('hint','')}" for a in agencies)
    prompt = (
        "You are a municipal operations analyst classifying local news for a city dashboard.\n"
        f"CIVIC TOPICS (choose 0+ that genuinely apply): {', '.join(topics_vocab)}.\n"
        f"AGENCY (choose the ONE most responsible; 'none' if not agency-specific):\n{ag_lines}\n"
        f"CIVIC CATEGORY (choose one): {', '.join(CATEGORIES)}.\n"
        "Each item is prefixed with [place] it was filed under. For EACH numbered item return a JSON object:\n"
        "  index (int), topics (array from the CIVIC TOPICS list — ONLY if the article is really about that "
        "topic; merely containing a word is NOT enough), agency_key (one key above), category (one civic "
        "category), severity (low|medium|high|critical), actionable (true only if a CITY AGENCY can act), "
        "place_relevant (true only if the article is actually ABOUT that [place]/its region — false for "
        "national/foreign/other-city news), one_line (<=15-word summary).\n"
        "Judge by MEANING, not keywords. Return ONLY a JSON array, no prose.\n\nITEMS:\n" + lines)
    # RuntimeError (not SystemExit) so the caller can skip a transiently-failed batch and keep going —
    # SystemExit is a BaseException that slips past `except Exception` and would kill a background run.
    try:
        content = io.ai([{"role": "user", "content": prompt}], 2500)
    except Exception as e:
        raise RuntimeError(f"model gateway unavailable ({e})")
    out = {}
    for r in _json_array(content):
        if isinstance(r, dict) and "index" in r:
            out[int(r["index"])] = r
    return out


def route(io, tenant, issue, role):
    key = issue["id"].rsplit("/", 1)[-1]
    assignee = f"role://{tenant}/{role}"
    case_id = f"ds:{tenant}:case/civic-{key}"; rec_id = f"ds:{tenant}:governance/recommendation/civic-{key}"
    task_id = f"ds:{tenant}:task/civic-{key}"; iid = f"ds:{tenant}:platform/inbox_item/civic-{key}"
    io.write("platform.case", case_id, {"schema": "platform.case", "id": case_id, "tenant": tenant,
             "title": f"Civic issue: {issue['title'][:80]}", "status": "open", "opened_at": NOW})
    io.write("governance.recommendation", rec_id, {"schema": "governance.recommendation", "id": rec_id, "tenant": tenant,
             "case": case_id, "for_task": task_id, "title": f"Route to {role}: {issue['title'][:80]}",
             "summary": issue.get("summary", ""), "proposed_action": f"Assign to {role} for action.",
             "produced_by": "ds:airawat:engine/news-classify", "confidence": issue.get("confidence"),
             "evidence": [issue.get("source_ref")], "created_at": NOW})
    io.write("platform.task", task_id, {"schema": "platform.task", "id": task_id, "tenant": tenant, "case": case_id,
             "verb": "decide", "title": f"[{issue['category']}/{issue.get('severity')}] {issue['title'][:90]}",
             "assignee": assignee, "actions": ["approve", "reject"], "status": "open", "created_at": NOW})
    io.write("platform.inbox_item", iid, {"schema": "platform.inbox_item", "id": iid, "tenant": tenant,
             "actor": assignee, "title": f"Decide: {issue['title'][:80]}", "ref": task_id, "kind": "task",
             "actions": ["approve", "reject"], "status": "unread", "created_at": NOW})


def classify(io, tenant, *, reclassify=False, maxn=120, batch=8, agency_map=None):
    """Core loop — LLM-classify the tenant's news.item, re-tag topics+agency+place onto each, route actionable."""
    if not tenant:
        raise SystemExit("FATAL: tenant not set — refusing to guess.")
    agency_map = {**_DEFAULT_MAP, **(agency_map or {})}
    route_sev = set((os.environ.get("ROUTE_SEVERITIES", "high,critical")).split(","))
    route_cat = set((os.environ.get("ROUTE_CATEGORIES", "law_and_order,disaster")).split(","))
    topics_vocab, agencies = load_taxonomy(io, tenant)
    agency_labels = {a["key"]: a.get("label", a["key"]) for a in agencies}; valid_keys = set(agency_labels)
    done = set()
    if not reclassify:
        for o in io.query("civic.issue"):
            if o.get("source_ref"): done.add(o["source_ref"])
    news = [o for o in io.query("news.item") if o.get("id") and o["id"] not in done]
    news.sort(key=lambda o: o.get("published_at") or o.get("observed_at") or "", reverse=True)
    news = news[:maxn]
    if not news:
        return {"classified": 0, "retagged": 0, "routed": 0}
    classified = retagged = routed = 0
    failed = 0
    for b in range(0, len(news), batch):
        chunk = news[b:b + batch]
        try:
            verdicts = classify_batch(io, chunk, topics_vocab, agencies, tenant)
        except Exception as e:                             # a transient LLM failure skips THIS batch, never the run
            failed += 1
            print(f"  ⚠ batch {b//batch} skipped: {str(e)[:120]}", flush=True)
            if failed > 20:                                # gateway is down, not a blip — stop (rollup still runs)
                print("  ✖ too many batch failures — stopping early", flush=True); break
            continue
        for i, it in enumerate(chunk):
            v = verdicts.get(i) or {}
            cat = v.get("category") if v.get("category") in CATEGORIES else "other"
            sev = (v.get("severity") or "low").lower()
            topics = [t for t in (v.get("topics") or []) if t in topics_vocab]
            akey = v.get("agency_key") if v.get("agency_key") in valid_keys else "none"
            place_ok = bool(v.get("place_relevant", True))
            item = {k: it.get(k) for k in ("schema", "id", "tenant", "title", "url", "summary", "source",
                    "source_ref", "published_at", "observed_at", "language", "image_url", "jurisdiction",
                    "ingest", "roles") if it.get(k) is not None}
            item.update({"schema": "news.item", "topics": topics, "agency_key": akey,
                         "agency": agency_labels.get(akey, ""), "place_relevant": place_ok,
                         "classified_by": METHOD, "observed_at": it.get("observed_at") or NOW})
            try:
                io.write("news.item", it["id"], item); retagged += 1
            except Exception as e:
                print(f"  ⚠ retag failed {it['id']}: {str(e)[:100]}")
            role = agency_map.get(cat, agency_map["other"])
            actionable = bool(v.get("actionable")) or cat in route_cat
            iid = f"ds:{tenant}:civic/issue/{hashlib.sha1(it['id'].encode()).hexdigest()[:16]}"
            issue = {"schema": "civic.issue", "id": iid, "tenant": tenant,
                     "title": (v.get("one_line") or it.get("title") or "")[:200],
                     "summary": (it.get("summary") or it.get("title") or "")[:600],
                     "category": cat, "jurisdiction": it.get("jurisdiction") or tenant, "severity": sev,
                     "agency": role, "source_ref": it["id"], "source_url": it.get("url"),
                     "status": "open", "confidence": 0.7, "method": METHOD, "observed_at": NOW}
            do_route = place_ok and actionable and (sev in route_sev or cat in route_cat)
            if do_route: issue["status"] = "routed"
            io.write("civic.issue", iid, issue); classified += 1
            if do_route: route(io, tenant, issue, role); routed += 1
        print(f"  classified {min(b+batch,len(news))}/{len(news)} · retagged {retagged} · routed {routed}", flush=True)
    print(f"[news-classify] retagged {retagged} news.item · {classified} civic.issue · routed {routed}", flush=True)
    try:
        rollup(io, tenant, agency_labels)                  # refresh the pre-computed analytics summary
    except Exception as e:
        print(f"[news-classify] rollup skipped: {str(e)[:120]}")
    return {"classified": classified, "retagged": retagged, "routed": routed}


def rollup(io, tenant, agency_labels=None):
    """ANALYTICS AS A BACKEND JOB: tally the tenant's classified news.item into one small news.analytics
    record (by topic, by agency, daily trend) so the News app reads a summary instead of aggregating the whole
    corpus client-side. Counts only place-relevant items (the local view)."""
    items = [o for o in io.query("news.item") if o.get("place_relevant") is not False]
    by_topic, by_agency, by_day = {}, {}, {}
    for o in items:
        for t in (o.get("topics") or []):
            by_topic[t] = by_topic.get(t, 0) + 1
        ak = o.get("agency_key")
        if ak and ak != "none":
            lbl = o.get("agency") or (agency_labels or {}).get(ak, ak)
            by_agency[lbl] = by_agency.get(lbl, 0) + 1
        d = (o.get("published_at") or o.get("observed_at") or "")[:10]
        if d:
            by_day[d] = by_day.get(d, 0) + 1
    trend = [{"date": d, "count": by_day[d]} for d in sorted(by_day)[-30:]]
    aid = f"ds:{tenant}:news/analytics"
    io.write("news.analytics", aid, {"schema": "news.analytics", "id": aid, "tenant": tenant,
             "generated_at": NOW, "total": len(items), "by_topic": by_topic, "by_agency": by_agency,
             "trend": trend, "method": METHOD})
    print(f"[news-classify] rollup: {len(items)} local items · {len(by_agency)} agencies · {len(by_topic)} topics")


def main():
    tenant = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT", "")
    try:
        amap = json.loads(os.environ.get("AGENCY_MAP", "") or "{}")
    except Exception:
        amap = {}
    res = classify(_broker_io(), tenant, reclassify=os.environ.get("RECLASSIFY", "0") == "1",
                   maxn=int(os.environ.get("MAX", "120")), batch=int(os.environ.get("BATCH", "8")), agency_map=amap)
    print("RESULT: " + json.dumps(res))


# HOST-DISPATCHED (app_host ENGINE_VERBS 'news-reclassify' → this): uses ctx.ai_complete (the host-held model
# gateway) + ctx.kfind/kwrite (attributed obo the admin, gated by this engine's grants). Admin clicks
# "Re-categorize" in the News app, or it runs on a schedule via the host.
def _reclassify(ctx):
    # The LLM is ~20s/batch, so a full-corpus reclassify far exceeds any HTTP/proxy window. Run it in the
    # BACKGROUND and return immediately. ctx.kwrite/kfind capture their auth headers at ctx-build time and
    # ai_complete is session-independent, so the thread keeps working after the request returns.
    body = ctx.get("body") or {}
    tenant = body.get("tenant") or ctx.get("tenant")
    io = _ctx_io(ctx)
    reclassify = bool(body.get("reclassify", True)); maxn = int(body.get("max", 1000)); batch = int(body.get("batch", 8))
    if body.get("sync"):                                   # small synchronous batch (tests / a few items)
        return classify(io, tenant, reclassify=reclassify, maxn=min(maxn, 16), batch=batch), None

    def _bg():
        try:
            r = classify(io, tenant, reclassify=reclassify, maxn=maxn, batch=batch)
            print(f"[news-classify] background reclassify done for {tenant}: {r}", flush=True)
        except Exception as e:
            print(f"[news-classify] background reclassify error for {tenant}: {str(e)[:200]}", flush=True)
    threading.Thread(target=_bg, daemon=True).start()
    return {"status": "started", "tenant": tenant, "max": maxn, "note": "reclassifying in background; poll news.item.classified_by"}, None


HANDLERS = {"news-reclassify": _reclassify}


if __name__ == "__main__":
    main()
