#!/usr/bin/env python3
"""rule-extract engine: knowledge.document chunks → governance.rule PROPOSALS via the LLM (kimi-k3).

The guideline→rule compiler — the bridge from the Knowledge track to the Rules track. Confined: reads the
document's chunks and calls the broker /v1/complete (pluggable model gateway) to extract enforceable
threshold rules, writing each as a governance.rule with status="proposed". Every proposal is CITED to the
exact source chunk (chunk_hash — resolvable + tamper-evident via /v1/resolve-citation) and inherits the
document's legal_force (a mandatory order ⇒ a mandatory rule). AI PROPOSES; a human approves before a rule
goes live to the rules-evaluation engine. Fails loudly if no chat provider is configured (no synthetic rules).
"""
import json, os, re, urllib.request
from datetime import datetime, timezone

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
T = os.environ.get("WRITE_TENANT", "kmc")
DOC_ID = os.environ.get("DOC_ID")   # a specific doc_id, or all docs when unset
CID = "ds:airawat:engine/rule-extract"

PROMPT = """You extract NORMATIVE RULES from an official document so a reviewer can approve them and, where they
are quantitative, a rules engine can enforce them. A normative rule is a threshold, requirement, prohibition, or
permission the document ESTABLISHES — never a definition, heading, background, or mere description.

Document: "{title}" — a {doc_class} issued by {issuer}; legal force: {force}.
Section: {label}
Text:
{text}

Output ONLY a JSON array. For each normative rule, an object with:
  "statement":  the rule in one clear sentence, faithful to the text.
  "rule_type":  one of "threshold" | "requirement" | "prohibition" | "permission".
  "subject":    the thing regulated (e.g. "residential density", "building height", "PM2.5", "fire safety NOC").
  "applies_to": a short "<domain>.<subject>" slug (e.g. "urban.density", "air.quality", "fire.safety").
  "condition":  when/where it applies, or null (e.g. "R1 residential zones", "buildings above 15 m").
  "action":     the mandated action/obligation, or null.
  If the rule is QUANTITATIVE, ALSO give: "metric" (machine name, e.g. "residential_density"),
  "operator" (one of >  >=  <  <=  ==  !=  between), "value" (a number, or [min,max] for between), "unit".
  If it is not quantitative, set metric, operator, value, unit to null.
For air-quality thresholds specifically, use applies_to="air.quality", metric="pm25", unit="µg/m³" and map any
AQI bands to the CPCB PM2.5 breakpoints (Good 0-30, Satisfactory 31-60, Moderate 61-90, Poor 91-120,
Very Poor 121-250, Severe 250+). If the section states no enforceable rule, output []."""


def _call(path, body, timeout=120):
    # Retry transient broker errors (5xx / connection); 4xx (e.g. no provider) raise immediately so the
    # caller can report it. The /v1/complete caller relies on that distinction.
    import time as _t
    for _a in range(4):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if e.code >= 500 and _a < 3:
                _t.sleep(1.5 * (_a + 1)); continue
            raise
        except urllib.error.URLError:
            if _a < 3:
                _t.sleep(1.5 * (_a + 1)); continue
            raise


def _slug(s):
    s = re.sub(r"[^a-z0-9.]+", ".", (s or "").lower()).strip(".")
    s = re.sub(r"\.{2,}", ".", s)
    return s or None


def _json_array(s):
    m = re.search(r"\[.*\]", s or "", re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except Exception:
        return []


def _section_label(loc):
    """Human label for a section from its locator — eId-aware (ADR-0057), legacy-compatible."""
    if loc.get("number"):
        h = loc.get("heading")
        return f"§{loc['number']}" + (f" {h}" if h and str(loc["number"]).count(".") <= 1 else "")
    return loc.get("label") or "section"


def main():
    all_docs = [r.get("object") or r for r in _call("/v1/query", {"scope": "knowledge.document", "limit": 500}).get("results", [])]
    targets = [d for d in all_docs if d.get("doc_id") == DOC_ID] if DOC_ID else all_docs   # DOC_ID scopes; unset = all
    if not targets:
        print(f"no knowledge.document{f' {DOC_ID!r}' if DOC_ID else ''}."); return 1
    versions = [r.get("object") or r for r in _call("/v1/query", {"scope": "knowledge.version", "limit": 3000}).get("results", [])]
    all_rules = [r.get("object") or r for r in _call("/v1/query", {"scope": "governance.rule", "limit": 5000}).get("results", [])]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cap = int(os.environ.get("MAX_EXTRACT_CHUNKS", "60"))
    grand = 0

    for doc in targets:
        did = doc["doc_id"]
        cur = next((v for v in versions if v.get("id") == doc.get("current_version")), None)
        if not cur:
            print(f"  {did}: no current version — run doc-chunk-version first."); continue
        # the CURRENT version's chunks only (content-addressed store keeps every version; the manifest is truth)
        want = {c["chunk_hash"]: c.get("locator", {}) for c in cur.get("chunks", [])}
        recs = {c["chunk_hash"]: c for c in
                [r.get("object") or r for r in _call("/v1/query", {"scope": "knowledge.chunk", "limit": 8000}).get("results", [])]
                if c.get("doc_id") == did and c["chunk_hash"] in want}
        chunks = [recs[h] for h in want if h in recs and want[h].get("eid") != "front" and want[h].get("label") != "preamble"]

        # (1) RE-LINK existing rules to the CURRENT content by their stable eId (ADR-0057). A rule extracted
        #     from §4.11.1 follows §4.11.1 across re-versioning — the eId is stable, only the chunk_hash (the
        #     integrity pin) moves. This preserves APPROVED rules; nothing is re-extracted or duplicated.
        eid2hash = {loc.get("eid"): h for h, loc in want.items() if loc.get("eid")}
        mine = [r for r in all_rules if (r.get("source") or {}).get("doc") == doc["id"]]
        relinked = 0
        for r in mine:
            src = r.get("source") or {}; eid = src.get("eid")
            if eid and eid in eid2hash and src.get("chunk_hash") != eid2hash[eid]:
                src["chunk_hash"] = eid2hash[eid]; src["doc_version"] = cur["id"]; r["source"] = src
                _call("/v1/write", {"scope": "governance.rule", "id": r["id"], "object": r,
                                    "valid_time": {"from": now}, "asserted_at": now})
                relinked += 1

        # (2) EXTRACT only for sections not already covered by a rule (idempotent — deterministic ids by eId,
        #     so a re-run overwrites rather than duplicating; and we skip the LLM for already-covered sections).
        covered = {e for e in ((r.get("source") or {}).get("eid") for r in mine) if e}
        todo = [c for c in chunks if c.get("locator", {}).get("eid") not in covered]
        if len(todo) > cap:
            stride = len(todo) / cap
            todo2 = [todo[int(i * stride)] for i in range(cap)]
            print(f"  {did}: {len(todo)} uncovered sections; sampling {cap} evenly (MAX_EXTRACT_CHUNKS).")
            todo = todo2
        n = 0; model = None
        for ch in todo:
            loc = ch.get("locator", {}); label = _section_label(loc); eid = loc.get("eid")
            prompt = PROMPT.format(title=doc.get("title"), doc_class=doc.get("doc_class", "document"),
                                   issuer=doc.get("issuer"), force=doc.get("legal_force"),
                                   label=label, text=ch.get("text", ""))
            try:
                res = _call("/v1/complete", {"messages": [{"role": "user", "content": prompt}]})
            except urllib.error.HTTPError as e:
                print(f"FAIL: /v1/complete → {e.code} (configure a chat provider + key)."); return 1
            model = res.get("model"); receipt = res.get("receipt")   # ADR-0058: the LLM interaction receipt
            for i, r in enumerate(_json_array(res.get("content", ""))):
                rtype = (r.get("rule_type") or "").lower(); statement = (r.get("statement") or "").strip()
                if rtype not in ("threshold", "requirement", "prohibition", "permission") or not statement:
                    continue
                quant = r.get("operator") and r.get("value") is not None
                subject = r.get("subject") or label
                applies = _slug(r.get("applies_to")) or ("air.quality" if r.get("metric") == "pm25" else "document.norm")
                rid = f"extracted-{did}-{re.sub(r'[^a-z0-9]+', '-', str(eid or label).lower()).strip('-')}-{i}"  # deterministic by eId
                obj = {"schema": "governance.rule", "id": f"ds:{T}:governance/rule/{rid}", "rule_id": rid,
                       "title": statement[:120], "statement": statement, "rule_type": rtype,
                       "applies_to": applies, "subject": subject, "stage": subject,
                       "metric": r.get("metric"), "operator": r.get("operator") if quant else None,
                       "value": r.get("value") if quant else None, "unit": r.get("unit"),
                       "condition": r.get("condition"), "proposed_action": r.get("action") or "",
                       "binding": doc.get("legal_force", "advisory"), "authority": doc.get("issuer"),
                       "status": "proposed", "scope": {"jurisdiction": (doc.get("jurisdiction") or {}).get("level", "national")},
                       "evaluable": bool(quant),
                       "source": {"doc": doc["id"], "doc_class": doc.get("doc_class"), "chunk_hash": ch["chunk_hash"],
                                  "eid": eid, "uri": loc.get("uri"), "doc_version": cur["id"],
                                  "citation": f"{doc.get('title')}, {label}", "derived_via": receipt},
                       "produced_by": {"component": CID, "model": model}, "tenant": T}
                _call("/v1/write", {"scope": "governance.rule", "id": obj["id"], "object": obj,
                                    "valid_time": {"from": now}, "asserted_at": now})
                n += 1
        grand += n
        print(f"  {did}: re-linked {relinked} existing rule(s) to current content; extracted {n} new "
              f"(model {model}); {len(covered)} eIds already covered.")

    print(f"rule-extract done: {grand} new PROPOSED rule(s) across {len(targets)} document(s). "
          f"Rules cite a stable eId + chunk_hash → they re-link to content on every re-version. Awaiting approval.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
