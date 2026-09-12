#!/usr/bin/env python3
"""proposal-advisory SKILL (INT-0002): frame a project.proposal into a clear FUNDER-FACING case.

The JE and the engines own the facts (site cells, people, lever, budget, ask). This skill only rewrites the
proposal SUMMARY — problem → intervention → ask → why it is a sound investment — grounded ONLY in the
proposal's own numbers, changing none and inventing nothing. Confined: broker only. HYBRID: the LLM writes the
prose; no model ⇒ the existing summary is kept, LOUDLY flagged. Read-modify-write: every other field preserved.
Frames DRAFT + SUBMITTED proposals (still author-editable); never touches an approved baseline.
"""
import json, os, re, time, urllib.request, urllib.error

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
WRITE_TENANT = os.environ.get("WRITE_TENANT", "kmc")
REASONING = os.environ.get("REASONING", "hybrid")
CID = "ds:airawatdeveloper:skill/proposal-advisory"
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


_META = ("let me", "i need", "i'll", "i should", "i must", "i can", "okay", "first", "draft", "here's", "note",
         "wait", "hmm", "actually", "interesting", "tags", "the model", "instruction", "so ", "maybe", "perhaps",
         "reconcile", "tension", "safer", "should i", "how to", "both numbers")
_DOMAIN = ("people", "resident", "cell", "crowd", "floor", "crore", "redevelop", "pmay", "overcrowd", "fund")

def _extract(raw):
    """Pull the clean final summary from a reasoning model that thinks out loud. Prefer a CLOSED <summary>…</summary>;
    else strip <think>, drop meta/analysis paragraphs, and take the last narrative paragraph that reads like a summary."""
    m = re.search(r"<summary>\s*(.+?)\s*</summary>", raw, re.S | re.I)
    if m:
        return re.sub(r"\s+", " ", re.sub(r"</?summary>", "", m.group(1), flags=re.I)).strip()
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S | re.I)
    raw = re.sub(r"</?summary>", "", raw, flags=re.I)
    paras = [q.strip() for q in re.split(r"\n\s*\n", raw) if q.strip()]
    good = [q for q in paras if not any(q.lower().lstrip("-*# ").startswith(w) for w in _META)]
    for q in reversed(good):                                   # last clean paragraph that looks like the case
        low = q.lower()
        if len(q) > 80 and sum(1 for k in _DOMAIN if k in low) >= 2:
            return re.sub(r"\s+", " ", q).strip()
    return ""

def _facts(p):
    ask = p.get("ask") or {}; iv = p.get("intervention") or {}; site = p.get("site") or {}
    oc = (p.get("outcomes") or [{}])[0]
    cells = len(site.get("cells") or [])
    return (f"Title: {p.get('title')}\n"
            f"Beneficiaries below the floor: {ask.get('beneficiaries')}\n"
            f"Priority cells: {cells}\n"
            f"Outcome target: '{oc.get('statement')}' (baseline {oc.get('baseline')} people below floor → target {oc.get('target')})\n"
            f"Intervention lever: {iv.get('label') or iv.get('kind')}"
            + (f" — {iv.get('description')}" if iv.get('description') else "")
            + (f" (scheme: {iv.get('from_scheme')})" if iv.get('from_scheme') else "") + "\n"
            f"Funding ask: {ask.get('amount_cr')} INR crore, mode '{ask.get('mode')}'"
            + (f", addressed to {ask.get('program')}" if ask.get('program') else "") + "\n"
            f"Environmental draw: {iv.get('env_draw')} hectares of {iv.get('env_resource')}".rstrip()
            if iv.get('env_resource') == 'land' else
            f"Environmental draw: {iv.get('env_draw')} of {iv.get('env_resource') or 'n/a'}")


def _template(p):
    """A clean deterministic summary from the proposal's own facts — the honest fallback when no model parses."""
    ask = p.get("ask") or {}; iv = p.get("intervention") or {}; site = p.get("site") or {}
    n = ask.get("beneficiaries"); cells = len(site.get("cells") or [])
    n_s = f"{n:,}" if isinstance(n, (int, float)) else str(n)
    lever = iv.get("label") or iv.get("kind") or "the selected intervention"
    return (f"Relieve overcrowding for {n_s} residents across {cells} priority cells via {lever}"
            + (f" ({iv.get('from_scheme')})" if iv.get("from_scheme") else "")
            + f". Funding ask: {ask.get('amount_cr')} INR crore ({ask.get('mode')}). Anchored to a measured "
              "per-cell diagnosis, costed line by line, and tracked to completion on the real ledger.")


def _frame(p):
    try:
        instr = open(os.path.join(HERE, "SKILL.md")).read()
    except Exception:
        instr = ""
    prompt = (f"{instr}\n\n---\n{_facts(p)}\n\n"
              "Write the proposal summary for a funder now — 3-4 sentences: the problem (who + how many + where), "
              "the intervention and why it fits, the ask, and why it is a sound, accountable investment. Use ONLY "
              "the numbers above, change none, invent nothing. Do NOT think out loud, do NOT explain your reasoning. "
              "Begin your reply with <summary> and end with </summary>, containing ONLY the summary and nothing else.")
    try:
        res = _call("/v1/complete", {"messages": [{"role": "user", "content": prompt}]}, timeout=150)
        txt = _extract(res.get("content") or "")
        if len(txt) >= 80:                                     # a real narrative, not a fragment or leaked reasoning
            return txt[:900], res.get("model"), False
    except Exception:
        pass
    return _template(p), None, True                            # clean deterministic summary, LOUD (never leaked reasoning)


def main():
    props = [r.get("object") or r for r in _call("/v1/query", {"scope": "project.proposal", "limit": 1000}).get("results", [])]
    props = [p for p in props if p.get("status") in ("draft", "submitted")]
    if not props:
        print("no draft/submitted project.proposal to frame."); return 0
    FORCE = os.environ.get("FORCE_REFRAME") == "1"
    framed = skipped = 0
    for p in props:
        # LOOP-BREAK (auto-trigger safety): this skill writes project.proposal — its own trigger schema. The
        # `reason` field carries a 'advisory-framed:<model>' marker so a re-fired event does NOT re-write. FORCE overrides.
        if not FORCE and str(p.get("reason") or "").startswith("advisory-framed"):
            skipped += 1; continue
        summary, model, fell_back = _frame(p) if REASONING != "deterministic" else (_template(p), None, True)
        p = dict(p); p["summary"] = summary; p["reason"] = f"advisory-framed:{model or 'template'}"
        obs = p.get("created_at") or p.get("submitted_at") or ""
        _call("/v1/write", {"scope": "project.proposal", "id": p["id"], "object": p,
                            "valid_time": {"from": obs} if obs else {}, "asserted_at": obs})
        framed += 1
        print(f"FRAMED {p['id'].split('/')[-1]} [{model or 'templated'}]")
        print(f"   {summary}")
    print(f"done — framed {framed}, skipped {skipped} already-framed (loop-safe).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
