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
DOC_ID = os.environ.get("DOC_ID", "caqm-grap-ncr")
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


def main():
    docs = [r.get("object") or r for r in _call("/v1/query", {"scope": "knowledge.document", "limit": 500}).get("results", [])]
    docs = [d for d in docs if d.get("doc_id") == DOC_ID]
    if not docs:
        print(f"no knowledge.document '{DOC_ID}'."); return 1
    doc = docs[0]
    chunks = [r.get("object") or r for r in _call("/v1/query", {"scope": "knowledge.chunk", "limit": 2000}).get("results", [])]
    chunks = [c for c in chunks if c.get("doc_id") == DOC_ID and c.get("locator", {}).get("label") != "preamble"]
    if not chunks:
        print(f"no chunks for '{DOC_ID}' (run doc-chunk-version first)."); return 0
    # Rule extraction is one LLM call per section — cap it so a huge document (e.g. 1500 sections) doesn't fire
    # thousands of calls. This is a LOUD cap, never a silent truncation: we report exactly what was covered.
    cap = int(os.environ.get("MAX_EXTRACT_CHUNKS", "60"))
    total = len(chunks)
    if total > cap:
        # sample EVENLY across the whole document, not the first N — a large document's real norms live in later
        # chapters, not its front matter. Loud, never silent: we say exactly how much was covered.
        stride = total / cap
        chunks = [chunks[int(i * stride)] for i in range(cap)]
        print(f"NOTE: {total} sections; sampling {cap} evenly across the document (MAX_EXTRACT_CHUNKS). "
              f"Raise the cap for exhaustive extraction of a large document.")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    n = 0; model = None
    for ch in chunks:
        label = ch["locator"]["label"]
        prompt = PROMPT.format(title=doc.get("title"), doc_class=doc.get("doc_class", "document"),
                               issuer=doc.get("issuer"), force=doc.get("legal_force"),
                               label=label, text=ch.get("text", ""))
        try:
            res = _call("/v1/complete", {"messages": [{"role": "user", "content": prompt}]})
        except urllib.error.HTTPError as e:
            print(f"FAIL: /v1/complete → {e.code} (configure a chat provider + key). Nothing written."); return 1
        model = res.get("model")
        rules = _json_array(res.get("content", ""))
        wrote = 0
        for i, r in enumerate(rules):
            rtype = (r.get("rule_type") or "").lower()
            statement = (r.get("statement") or "").strip()
            # a rule must be a real normative statement of a known kind (not a definition/description)
            if rtype not in ("threshold", "requirement", "prohibition", "permission") or not statement:
                continue
            quant = r.get("operator") and r.get("value") is not None    # machine-evaluable?
            subject = r.get("subject") or r.get("stage") or label
            applies = _slug(r.get("applies_to")) or ("air.quality" if r.get("metric") == "pm25" else "document.norm")
            rid = f"extracted-{DOC_ID}-{re.sub(r'[^a-z0-9]+', '-', str(label).lower()).strip('-')}-{i}"
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
                              "citation": f"{doc.get('title')}, {label}"},
                   "produced_by": {"component": CID, "model": model}, "tenant": T}
            _call("/v1/write", {"scope": "governance.rule", "id": obj["id"], "object": obj,
                                "valid_time": {"from": now}, "asserted_at": now})
            wrote += 1; n += 1
        print(f"  {label}: {wrote} rule(s) extracted + cited")

    print(f"extracted {n} PROPOSED governance.rule from '{doc.get('title')}' (model {model}); "
          f"each cited to a chunk_hash, binding={doc.get('legal_force')}. Awaiting human approval → live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
