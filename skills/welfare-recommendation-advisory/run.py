#!/usr/bin/env python3
"""welfare-recommendation-advisory SKILL (INT-0002): frame welfare recommendations into clear human language.

The prioritisation ENGINE owns the maths and writes each governance.recommendation (persons, cells, lever,
budget). This skill only RE-PHRASES the title + summary so a councillor or citizen can read them — grounded
ONLY in the recommendation's own stated facts, changing no number and inventing nothing. Confined: broker only.
HYBRID: the LLM writes the prose; no model ⇒ the engine's deterministic text is kept, LOUDLY flagged.
Read-modify-write: every other field (evidence, score, method, produced_by) is preserved untouched.
"""
import json, os, re, time, urllib.request, urllib.error

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
WRITE_TENANT = os.environ.get("WRITE_TENANT", "kmc")
REASONING = os.environ.get("REASONING", "hybrid")
ONLY = os.environ.get("REC_FILTER", "welfare-cells")     # which recommendations to frame (id substring)
CID = "ds:airawatdeveloper:skill/welfare-recommendation-advisory"
VER = "0.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))


def _call(path, body, timeout=60):
    for a in range(4):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if e.code >= 500 and a < 3:
                time.sleep(1.5 * (a + 1)); continue
            raise
        except urllib.error.URLError:
            if a < 3:
                time.sleep(1.5 * (a + 1)); continue
            raise


def _clean(txt):
    m = re.search(r"<advisory>\s*(.+?)\s*</advisory>", txt, re.S | re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S | re.I).strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n", txt) if p.strip()]
    META = ("let me", "i need", "i'll", "i should", "okay", "first", "draft", "here's", "note:", "wait", "hmm")
    good = [p for p in paras if not any(p.lower().startswith(w) for w in META)]
    return re.sub(r"\s+", " ", (good[-1] if good else txt)).strip()[:700]


def _frame(rec):
    """LLM rewrites (title, summary) from the recommendation's OWN facts. Returns (title, summary, model, fell_back)."""
    facts = (f"Current title: {rec.get('title')}\nCurrent summary: {rec.get('summary')}\n"
             f"Full rationale (authoritative, has every number): {rec.get('rationale')}\n"
             f"Proposed action: {rec.get('proposed_action')}")
    try:
        instr = open(os.path.join(HERE, "SKILL.md")).read()
    except Exception:
        instr = ""
    prompt = (f"{instr}\n\n---\n{facts}\n\n"
              "Rewrite for a busy councillor, using ONLY the numbers above (change none, add none, get the "
              "direction right — overcrowding is RELIEVED, not raised). Output EXACTLY these two tagged lines and "
              "nothing else — no analysis, no preamble:\n"
              "<title>a clear headline, <=70 chars</title>\n"
              "<summary>2 sentences: what to do + where + the funded scale + the binding constraint</summary>")
    try:
        res = _call("/v1/complete", {"messages": [{"role": "user", "content": prompt}]}, timeout=150)
        raw = (res.get("content") or "")
        mt = re.search(r"<title>\s*(.+?)\s*</title>", raw, re.S | re.I)
        ms = re.search(r"<summary>\s*(.+?)\s*</summary>", raw, re.S | re.I)
        t = re.sub(r"\s+", " ", mt.group(1)).strip() if mt else ""
        s = re.sub(r"\s+", " ", ms.group(1)).strip() if ms else ""
        if t and s:
            return t[:120], s[:700], res.get("model"), False
    except Exception:
        pass
    return rec.get("title"), rec.get("summary"), None, True    # keep the engine's deterministic text, LOUD


def main():
    recs = [r.get("object") or r for r in _call("/v1/query", {"scope": "governance.recommendation", "limit": 1000}).get("results", [])]
    recs = [r for r in recs if ONLY in (r.get("id") or "")]
    if not recs:
        print(f"no governance.recommendation matching '{ONLY}' — nothing to frame."); return 0
    FORCE = os.environ.get("FORCE_REFRAME") == "1"
    framed = skipped = 0
    for rec in recs:
        # LOOP-BREAK (auto-trigger safety): this skill writes governance.recommendation — the same schema that
        # fires it. If it's already framed, do NOT write again, so the event chain terminates. FORCE re-frames.
        if not FORCE and "[framed by" in (rec.get("rationale") or ""):
            skipped += 1; continue
        title, summary, model, fell_back = _frame(rec) if REASONING != "deterministic" else (rec.get("title"), rec.get("summary"), None, True)
        rec = dict(rec)                                        # preserve every field; only re-phrase these two
        rec["title"] = title; rec["summary"] = summary
        base_rat = re.sub(r"\s*\[framed by[^\]]*\]\s*$", "", rec.get("rationale") or "").rstrip()
        rec["rationale"] = base_rat + (f" [framed by the {model}]" if model else " [framing model unavailable — engine text kept]")
        obs = rec.get("created_at") or ""
        _call("/v1/write", {"scope": "governance.recommendation", "id": rec["id"], "object": rec,
                            "valid_time": {"from": obs} if obs else {}, "asserted_at": obs})
        framed += 1
        print(f"FRAMED {rec['id'].split('/')[-1]} [{model or 'templated'}]: {title}")
        print(f"   {summary}")
    print(f"done — framed {framed}, skipped {skipped} already-framed (loop-safe).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
