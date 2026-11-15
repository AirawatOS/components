"""model-eval — test language models on the platform's OWN tasks, through the same path production uses.

Two jobs, one engine:

  RUN (default): take the oldest pending intelligence.eval_run; for every model × active case of its suite, send the
  case's messages to the broker's /v1/complete NAMING the model (the gateway routes by task + model, exactly as for a
  production call), score the answer, write intelligence.eval_result, and finish the run with a per-model summary.
  The gateway's reply says which model actually served it (`served_by`) — a name the gateway does not know is
  recorded as such, never silently credited to the requested model.

  SEED (SEED_DOC set): build a norm-extraction suite from a knowledge.document — one case per section that mentions
  SEED_FOCUS, its messages built EXACTLY as engine/rule-extract builds them (same prompt, same answer budget). The
  expected answer is taken from APPROVED governance.rule records for that section; until someone approves norms, a
  case is unlabelled and is scored on what needs no answer key (answered, valid, every number found in the text).

Scorers: norms · json_fields · contains · exact · grounded. Deterministic — no model judges another here.
"""
import json, os, re, sys, time, urllib.error, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ.get("RUN_TOKEN", "")
T = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""
CID, VER = "ds:airawatdeveloper:engine/model-eval", "0.1.2"
MAX_CASES = int(os.environ.get("MAX_CASES") or 60)
CASES = (os.environ.get("CASES") or "all").strip().lower()   # all | with_norms (labelled cases expecting ≥ 1 norm)


MAX_TOKENS = int(os.environ.get("MAX_TOKENS") or 0)   # run-level answer budget; 0 = each case's own max_tokens


def budget(case):
    """The answer budget for one call: the run's MAX_TOKENS when set (thinking models spend part of it reasoning, so a
    case's own budget can cut their answer off), else the case's max_tokens."""
    return MAX_TOKENS or int(case.get("max_tokens") or 1024)


NOTE_NORMS = "cases with expected norms only"
THINKING = (os.environ.get("THINKING") or "").strip().lower()    # on | off | "" (the model's own default)


def thinking_arg():
    """The gateway's per-request thinking switch for this run: on / off, or nothing (the model's default)."""
    return {"thinking": THINKING == "on"} if THINKING in ("on", "off") else {}
STOP_AFTER = int(os.environ.get("STOP_AFTER") or 1400)   # seconds; the runtime stops a run at 1,700 s, one call can take 240 s


def has_norms(case):
    return bool(case.get("labelled")) and bool(((case.get("expected") or {}).get("norms")))
OPS = (">", ">=", "<", "<=", "==", "!=", "between")
APPROVED = ("approved", "live", "active")
TASKS = {}                                        # model → the gateway task that serves it (found on first call)

# rule-extract 0.1.4's prompt, verbatim — a seeded case must be the call production makes (re-seed if it changes)
NORM_PROMPT = """Answer IMMEDIATELY with the JSON array — do not deliberate at length; if unsure about a statement, leave it out.
You extract NORMATIVE RULES from an official document so a reviewer can approve them and, where they
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


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def call(path, body, timeout=180):
    for a in range(4):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Run-Token": TOKEN})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code >= 500 and a < 3 and path != "/v1/complete":
                time.sleep(2 * (a + 1)); continue
            raise
        except urllib.error.URLError:
            if a < 3:
                time.sleep(2 * (a + 1)); continue
            raise


def mine(scope, limit=20000):
    rows = [r.get("object") or r for r in (call("/v1/query", {"scope": scope, "limit": limit}).get("results") or [])]
    return [o for o in rows if str(o.get("id", "")).startswith("ds:%s:" % T)]


def write(scope, obj):
    t = now()
    obj = {k: v for k, v in obj.items() if v is not None}
    call("/v1/write", {"scope": scope, "id": obj["id"], "object": obj, "valid_time": {"from": t}, "asserted_at": t})


def slug(s, n=60):
    return (re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:n]) or "x"


# ---- scoring ---------------------------------------------------------------------------------------------------
def json_array(s):
    m = re.search(r"\[.*\]", s or "", re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, list) else None
    except Exception:
        return None


SCALE = {"lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "crore": 1e7, "crores": 1e7, "cr": 1e7}
_NUM = r"(\d{1,3}(?:,\d{2,3})+|\d+)(?:\.(\d+))?"


def _f(whole, frac):
    return float(whole.replace(",", "") + ("." + frac if frac else ""))


def numbers_in(text):
    """Every number written in a text, as floats (15,000 → 15000) — and, for the Indian number words, its plain value
    too: '10 lakh' → 1,000,000, '2.5 crore' → 25,000,000, '5-8 lakh' / '5 to 8 lakh' → 500,000 and 800,000. A model
    that writes a lakh figure as a plain number has not invented it."""
    t = text or ""
    out = set()
    for m in re.finditer(r"(?<![\w.])" + _NUM, t):
        try:
            out.add(_f(m.group(1), m.group(2)))
        except ValueError:
            pass
    rng = r"(?<![\w.])" + _NUM + r"(?:\s*(?:-|–|to)\s*" + _NUM + r")?\s*(lakhs?|lacs?|crores?|cr)\b"
    for m in re.finditer(rng, t, re.I):
        k = SCALE[m.group(5).lower()]
        try:
            out.add(_f(m.group(1), m.group(2)) * k)
            if m.group(3):
                out.add(_f(m.group(3), m.group(4)) * k)
        except ValueError:
            pass
    return out


def vals(v):
    v = v if isinstance(v, list) else [v]
    return [float(x) for x in v if isinstance(x, (int, float)) and not isinstance(x, bool)]


def close(a, b):
    return abs(a - b) <= max(1e-9, 0.01 * max(abs(a), abs(b)))


def score_norms(case, content):
    src = " ".join(m.get("content", "") for m in case.get("messages") or [] if isinstance(m, dict))
    items = json_array(content)
    answered = bool((content or "").strip())
    m = {"answered": answered, "parsed": items is not None, "items": len(items or [])}
    valid = [r for r in (items or []) if isinstance(r, dict) and r.get("metric") and r.get("operator") in OPS and vals(r.get("value"))]
    nums = numbers_in(src)
    grounded = [r for r in valid if all(any(close(x, n) for n in nums) for x in vals(r.get("value")))]
    m.update(valid=len(valid), grounded=len(grounded),
             norms=[{k: r.get(k) for k in ("metric", "operator", "value", "unit")} for r in valid][:60],
             not_grounded=[{k: r.get(k) for k in ("metric", "value", "unit")} for r in valid if r not in grounded][:20])
    exp = case.get("expected") or {}
    if case.get("labelled") and isinstance(exp.get("norms"), list):
        gold = exp["norms"]
        hit = 0; used = set()
        for g in gold:
            gv = vals(g.get("value"))
            for i, r in enumerate(valid):
                if i in used:
                    continue
                rv = vals(r.get("value"))
                if gv and len(gv) == len(rv) and all(close(a, b) for a, b in zip(sorted(gv), sorted(rv))):
                    hit += 1; used.add(i); break
        recall = hit / len(gold) if gold else (1.0 if not valid else 0.0)
        precision = hit / len(valid) if valid else (1.0 if not gold else 0.0)
        m.update(recall=round(recall, 3), precision=round(precision, 3), gold=len(gold))
        sc = 0.0 if (recall + precision) == 0 else 2 * recall * precision / (recall + precision)
    else:                                   # unlabelled: answered, parsable, and every number found in the text
        sc = 0.0 if not (answered and items is not None) else (len(grounded) / len(valid) if valid else 1.0)
    return round(sc, 3), m


def last_json_object(text):
    """The LAST JSON object in a text: a model that shows its working before the answer may use braces in the
    working, so the answer is the final object that parses."""
    dec = json.JSONDecoder()
    for i in range(len(text) - 1, -1, -1):
        if text[i] == "{":
            try:
                obj, _ = dec.raw_decode(text, i)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def field_equal(got, want):
    """One expected JSON field: numbers within 1% (4.5 = 4.50 = "4.5"), everything else as case-insensitive text."""
    g, w = _num(got), _num(want)
    if g is not None and w is not None:
        return close(g, w)
    return str(got).strip().lower() == str(want).strip().lower()


def score_case(case, content):
    sc = case.get("scorer") or "norms"
    exp = case.get("expected") or {}
    if sc == "norms":
        return score_norms(case, content)
    text = (content or "").strip()
    if sc == "exact":
        ok = text == str(exp.get("answer", "")).strip()
        return (1.0 if ok else 0.0), {"answered": bool(text)}
    if sc == "contains":
        want = [w for w in exp.get("contains") or [] if w]
        got = [w for w in want if w.lower() in text.lower()]
        return (round(len(got) / len(want), 3) if want else 0.0), {"answered": bool(text), "found": len(got), "of": len(want)}
    if sc == "json_fields":
        obj = last_json_object(text)
        fields = exp.get("fields") or {}
        ok = [k for k, v in fields.items() if isinstance(obj, dict) and k in obj and field_equal(obj[k], v)]
        return (round(len(ok) / len(fields), 3) if fields else 0.0), {"answered": bool(text), "parsed": isinstance(obj, dict),
                "matched": len(ok), "of": len(fields), "wrong": [k for k in fields if k not in ok][:20]}
    if sc == "grounded":
        src = " ".join(m.get("content", "") for m in case.get("messages") or [] if isinstance(m, dict))
        out, ins = numbers_in(text), numbers_in(src)
        bad = [x for x in out if not any(close(x, n) for n in ins)]
        return (0.0 if not text else round(1 - len(bad) / max(1, len(out)), 3)), {"answered": bool(text), "numbers": len(out), "not_in_input": len(bad)}
    return 0.0, {"error": "unknown scorer %s" % sc}


# ---- seed a suite from a document ------------------------------------------------------------------------------
def seed():
    doc_id, suite = os.environ["SEED_DOC"], os.environ.get("SEED_SUITE") or ("norms-" + slug(os.environ["SEED_DOC"], 40))
    focus = [f.strip().lower() for f in (os.environ.get("SEED_FOCUS") or "").split(",") if f.strip()]
    limit = int(os.environ.get("SEED_LIMIT") or 30)
    doc = next((d for d in mine("knowledge.document", 2000) if d.get("doc_id") == doc_id), None)
    if not doc:
        raise SystemExit("no knowledge.document %r in tenant %s" % (doc_id, T))
    ver = next((v for v in mine("knowledge.version", 5000) if v.get("id") == doc.get("current_version")), None)
    if not ver:
        raise SystemExit("document %r has no current version yet (not sectioned)" % doc_id)
    locs = {c["chunk_hash"]: c.get("locator", {}) for c in ver.get("chunks", [])}
    chunks = [c for c in mine("knowledge.chunk", 20000) if c.get("doc_id") == doc_id and c.get("chunk_hash") in locs]
    chunks = [c for c in chunks if locs[c["chunk_hash"]].get("eid") != "front"]
    if focus:
        chunks = [c for c in chunks if any(f in (c.get("text") or "").lower() for f in focus)]
    if len(chunks) > limit:
        step = len(chunks) / limit
        chunks = [chunks[int(i * step)] for i in range(limit)]
    rules = [r for r in mine("governance.rule", 10000) if (r.get("source") or {}).get("doc") == doc["id"]
             and str(r.get("status") or "").lower() in APPROVED]
    made = labelled = 0
    for c in chunks:
        loc = locs[c["chunk_hash"]]
        label = ("§%s %s" % (loc.get("number"), loc.get("heading") or "")).strip() if loc.get("number") else (loc.get("label") or "section")
        prompt = NORM_PROMPT.format(title=doc.get("title"), doc_class=doc.get("doc_class", "document"), issuer=doc.get("issuer"),
                                    force=doc.get("legal_force"), label=label, text=c.get("text", ""))
        gold = [{"metric": r.get("metric"), "operator": r.get("operator"), "value": r.get("value"), "unit": r.get("unit"),
                 "statement": r.get("statement") or r.get("title")}
                for r in rules if (r.get("source") or {}).get("chunk_hash") == c["chunk_hash"]]
        cid = "ds:%s:intelligence/eval_case/%s--%s" % (T, slug(suite, 40), slug(loc.get("eid") or c["chunk_hash"][-12:], 60))
        case = {"schema": "intelligence.eval_case", "id": cid, "tenant": T, "suite": suite, "task": "extract",
                "title": "%s — %s" % (doc.get("title"), label)[:200],
                "messages": [{"role": "user", "content": prompt}], "max_tokens": 2500, "scorer": "norms",
                "expected": {"norms": gold} if gold else None, "labelled": bool(gold),
                "source": {"kind": "document-section", "doc": doc["id"], "chunk_hash": c["chunk_hash"], "eid": loc.get("eid"),
                           "label": label, "seeded_by": CID},
                "tags": ["norm-extraction", doc_id], "visibility": "public" if doc.get("doc_class") == "guideline" else "private",
                "status": "active", "created_by": "engine/model-eval", "created_at": now()}
        write("intelligence.eval_case", case); made += 1; labelled += bool(gold)
    print("seeded suite %r from %s: %d cases (%d labelled from approved norms)" % (suite, doc_id, made, labelled), flush=True)
    print("RESULT: " + json.dumps({"seeded": made, "labelled": labelled, "suite": suite}))
    return 0


# ---- run -------------------------------------------------------------------------------------------------------
OUT_MAX = 40000           # a full answer (was 6,000: long answers were cut and their later norms lost)
OLD_CAP = 6000


def ask(model, c):
    """One case to one model through the broker → (output, served_by, usage, error, ms)."""
    t0 = time.time(); out = ""; served = None; usage = None; err = None
    try:
        res = None
        for task in TASKS.get(model, ("chat", "eval")):   # 'eval' = models registered for evaluation only —
            try:                                          # never on a production route (a call naming no model)
                res = call("/v1/complete", {"messages": c["messages"], "max_tokens": budget(c),
                                            "model": model, "task": task, "receipt": False, **thinking_arg()}, timeout=240)
                TASKS[model] = (task,)
                break
            except urllib.error.HTTPError as e:
                if e.code == 501 and task != "eval":      # not served for this task — try the next
                    continue
                raise
        out, served, usage = res.get("content") or "", res.get("model"), res.get("usage")
    except urllib.error.HTTPError as e:
        err = "HTTP %s: %s" % (e.code, (e.read() or b"")[:200].decode(errors="replace"))
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, str(e)[:200])
    return out, served, usage, err, int((time.time() - t0) * 1000)


def result_record(run, rslug, model, c, out, served, usage, err, ms, sc, met, rescored=None):
    rec = {"schema": "intelligence.eval_result",
           "id": "ds:%s:intelligence/eval_result/%s--%s--%s" % (T, rslug, slug(model, 30), slug(c["id"].rsplit("/", 1)[-1], 70)),
           "tenant": T, "run_ref": run["id"], "case_ref": c["id"], "suite": run["suite"], "model": model, "served_by": served,
           "output": (out or "")[:OUT_MAX], "score": sc, "metrics": met, "latency_ms": ms, "usage": usage, "error": err, "at": now()}
    if rescored:
        rec["metrics"] = dict(met, rescored=rescored)
    return rec


def summarise(run, results):
    summary = {}
    for model in run.get("models") or []:
        rs = [r for r in results if r.get("model") == model]
        n = max(1, len(rs)); sb = {}
        for r in rs:
            if r.get("served_by"):
                sb[r["served_by"]] = sb.get(r["served_by"], 0) + 1
        mets = [r.get("metrics") or {} for r in rs]
        summary[model] = {"cases": len(rs), "answered": sum(1 for m in mets if m.get("answered")),
                          "empty": sum(1 for r, m in zip(rs, mets) if not r.get("error") and not m.get("answered")),
                          "errors": sum(1 for r in rs if r.get("error")),
                          "score": round(sum(float(r.get("score") or 0) for r in rs) / n, 3),
                          "valid": sum(int(m.get("valid") or 0) for m in mets), "grounded": sum(int(m.get("grounded") or 0) for m in mets),
                          "avg_latency_ms": int(sum(int(r.get("latency_ms") or 0) for r in rs) / n),
                          "avg_completion_tokens": int(sum(int((r.get("usage") or {}).get("completion_tokens") or 0) for r in rs) / n),
                          "served_by": sb}
    return summary


def rescore(run_id, rerun_cut):
    """Score a stored run again with the current scorers and labels — no model calls, except (RERUN_TRUNCATED=1) the
    answers stored cut at the old 6,000-character limit, which are asked again in full."""
    run = next((r for r in mine("intelligence.eval_run") if r["id"] == run_id), None)
    if not run:
        raise SystemExit("no eval run %r" % run_id)
    cases = {c["id"]: c for c in mine("intelligence.eval_case")}
    results = [r for r in mine("intelligence.eval_result") if r.get("run_ref") == run_id]
    rslug = slug(run_id.rsplit("/", 1)[-1], 40); redone = rerun = kept = 0; new = []
    for r in results:
        c = cases.get(r.get("case_ref"))
        if not c:
            new.append(r); continue
        out, served, usage, err, ms = r.get("output") or "", r.get("served_by"), r.get("usage"), r.get("error"), r.get("latency_ms")
        how = "rescored"
        was_cut = len(out) == OLD_CAP and not str(((r.get("metrics") or {}).get("rescored") or {}).get("how", "")).startswith("re-asked")
        if not err and was_cut:
            if not rerun_cut:                      # a cut answer would score lower than the full one did — keep the original
                new.append(r); kept += 1; continue
            out, served, usage, err, ms = ask(r["model"], c); how = "re-asked (answer had been cut at 6,000 characters)"; rerun += 1
        sc, met = (0.0, {"answered": False}) if err else score_case(c, out)
        rec = result_record(run, rslug, r["model"], c, out, served, usage, err, ms, sc, met, rescored={"at": now(), "how": how, "was": r.get("score")})
        write("intelligence.eval_result", rec); new.append(rec); redone += 1
    summary = summarise(run, new)
    write("intelligence.eval_run", dict(run, summary=summary, note=((run.get("note") or "") + " · rescored " + now())[:500]))
    print("rescored %s: %d results (%d re-asked, %d cut answers kept as scored)" % (run_id, redone, rerun, kept), flush=True)
    print("RESULT: " + json.dumps({"run": run_id, "summary": summary}))
    return 0


def run_settings(run):
    """CASES and MAX_TOKENS for a run: the environment when given, else what the run's note recorded when it first
    started — so a run resumed by a later invocation keeps the settings it began with."""
    note = run.get("note") or ""
    cases = CASES if os.environ.get("CASES") else ("with_norms" if NOTE_NORMS in note else "all")
    m = re.search(r"answer budget (\d+) tokens", note)
    budget_ = MAX_TOKENS or (int(m.group(1)) if m else 0)
    t = re.search(r"thinking (on|off)", note)
    thinking = THINKING if THINKING in ("on", "off") else (t.group(1) if t else "")
    return cases, budget_, thinking


def run_one(run):
    global MAX_TOKENS, THINKING
    t0 = time.time()
    cases_mode, MAX_TOKENS, THINKING = run_settings(run)
    note = run.get("note") or ""
    if cases_mode == "with_norms" and NOTE_NORMS not in note:
        note = (note + " · " + NOTE_NORMS).lstrip(" ·")
    if MAX_TOKENS and "answer budget" not in note:
        note = (note + " · answer budget %d tokens" % MAX_TOKENS).lstrip(" ·")
    if THINKING in ("on", "off") and "thinking o" not in note:
        note = (note + " · thinking " + THINKING).lstrip(" ·")
    run = dict(run, note=note[:500] or None)
    cases = [c for c in mine("intelligence.eval_case") if c.get("suite") == run["suite"] and c.get("status") == "active"]
    if cases_mode == "with_norms":          # only labelled cases that expect at least one norm — cheaper, but it no
        cases = [c for c in cases if has_norms(c)]   # longer tests whether a model invents norms where there are none
    cases = sorted(cases, key=lambda c: c["id"])[:MAX_CASES]
    run = dict(run, started_at=run.get("started_at") or now())
    write("intelligence.eval_run", dict(run, status="running", error=None))
    if not cases:
        write("intelligence.eval_run", dict(run, status="failed", finished_at=now(),
              error="the suite has no active cases" + (" with expected norms" if cases_mode == "with_norms" else "")))
        return
    rslug = slug(run["id"].rsplit("/", 1)[-1], 40)
    recs = [r for r in mine("intelligence.eval_result") if r.get("run_ref") == run["id"]]   # a resumed run's results so far
    have = {(r.get("model"), r.get("case_ref")) for r in recs}
    todo = [(m, c) for m in run.get("models") or [] for c in cases if (m, c["id"]) not in have]
    print("run %s: %d of %d answers already in, %d to go" % (run["id"], len(have), len(have) + len(todo), len(todo)), flush=True)
    for i, (model, c) in enumerate(todo):
        if time.time() - t0 > STOP_AFTER:            # leave before the runtime's limit; the next invocation resumes
            left = len(todo) - i
            write("intelligence.eval_run", dict(run, status="pending", summary=summarise(run, recs), error=None,
                  note=(note + " · paused with %d answers to go" % left)[:500] if "paused with" not in note else note))
            print("PAUSED: %d answers to go — the next run of the engine resumes it" % left, flush=True)
            print("RESULT: " + json.dumps({"run": run["id"], "paused": True, "to_go": left}))
            return
        out, served, usage, err, ms = ask(model, c)
        sc, met = (0.0, {"answered": False}) if err else score_case(c, out)
        rec = result_record(run, rslug, model, c, out, served, usage, err, ms, sc, met)
        write("intelligence.eval_result", rec); recs.append(rec)
        print("  %s · %s: score %.2f · %d ms%s" % (model, c.get("title", "")[-60:], sc, ms, " · " + err[:80] if err else ""), flush=True)
    summary = summarise(run, recs)                 # from what was written, not a re-query (read-after-write lag)
    write("intelligence.eval_run", dict(run, status="done", finished_at=now(), summary=summary, error=None,
          note=re.sub(r" · paused with \d+ answers to go", "", note)[:500] or None))
    print("RESULT: " + json.dumps({"run": run["id"], "summary": summary}))


def main():
    if not T:
        raise SystemExit("FATAL: no tenant for this run (WRITE_TENANT / TENANT)")
    if os.environ.get("SEED_DOC"):
        return seed()
    if os.environ.get("RESCORE_RUN"):
        return rescore(os.environ["RESCORE_RUN"], os.environ.get("RERUN_TRUNCATED") == "1")
    pending = sorted([r for r in mine("intelligence.eval_run") if r.get("status") == "pending"], key=lambda r: str(r.get("requested_at") or ""))
    print("tenant=%s · %d pending run(s)" % (T, len(pending)), flush=True)
    if pending:
        run_one(pending[0])                     # one run per invocation; the schedule / the next Run takes the next
    return 0


if __name__ == "__main__":
    sys.exit(main())
