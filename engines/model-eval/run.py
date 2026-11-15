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
import base64, json, os, re, sys, time, urllib.error, urllib.request

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


def _answer(content):
    return last_json_object((content or "").strip())


def score_counts(case, content):
    """Counts per class (ADR-0134 B03/B04): expected.counts {class: n}; the answer's "counts" object (or the
    object itself). Score = 1 − total absolute error / total expected (floor 0); metrics: per-class error, MAE,
    exact-count accuracy and bias (+ over-counting, − under-counting)."""
    want = ((case.get("expected") or {}).get("counts") or {})
    obj = _answer(content); got = (obj.get("counts") if isinstance(obj, dict) and isinstance(obj.get("counts"), dict) else obj) or {}
    if not isinstance(got, dict):
        got = {}
    errs = {}
    for k, v in want.items():
        g = _num(got.get(k)) if k in got else 0.0
        errs[k] = (g if g is not None else 0.0) - float(v)
    n = max(1, len(want)); total = sum(abs(e) for e in errs.values()); base = max(1.0, float(sum(want.values())))
    m = {"answered": bool((content or "").strip()), "parsed": isinstance(obj, dict), "per_class_error": errs,
         "mae": round(total / n, 3), "exact": round(sum(1 for e in errs.values() if abs(e) < 0.5) / n, 3),
         "bias": round(sum(errs.values()) / n, 3)}
    return (round(max(0.0, 1 - total / base), 3) if m["parsed"] else 0.0), m


def score_labels(case, content):
    """Class labels (ADR-0134 A01 A02 A04 A06 B01 E03): expected.labels — a list (multi-label) or one string; the
    answer's "labels" (list) or "label" (string). Score = F1 between the sets; "uncertain" is an ordinary label, so
    answering "uncertain" where the reference says so scores, and anywhere else does not."""
    exp = (case.get("expected") or {}).get("labels")
    want = {str(x).strip().lower() for x in (exp if isinstance(exp, list) else [exp] if exp else [])}
    obj = _answer(content); got = set()
    if isinstance(obj, dict):
        v = obj.get("labels", obj.get("label"))
        got = {str(x).strip().lower() for x in (v if isinstance(v, list) else [v] if v not in (None, "") else [])}
    hit = len(want & got)
    p = hit / len(got) if got else (1.0 if not want else 0.0)
    r = hit / len(want) if want else (1.0 if not got else 0.0)
    f1 = 0.0 if p + r == 0 else 2 * p * r / (p + r)
    return round(f1, 3), {"answered": bool((content or "").strip()), "parsed": isinstance(obj, dict), "expected": sorted(want),
                          "got": sorted(got), "precision": round(p, 3), "recall": round(r, 3)}


def _cer(a, b):
    a, b = a or "", b or ""
    if not b:
        return 0.0 if not a else 1.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return min(1.0, prev[-1] / len(b))


def score_text(case, content):
    """Text read from the image (ADR-0134 C05): expected.text {field: string}; the answer's fields of the same name.
    Compared ignoring case and repeated spaces. Score = 1 − mean character error rate; metrics: CER per field and
    exact-match share."""
    want = ((case.get("expected") or {}).get("text") or {})
    obj = _answer(content) or {}
    norm = lambda x: " ".join(str(x or "").lower().split())
    cers = {k: round(_cer(norm(obj.get(k)) if isinstance(obj, dict) else "", norm(v)), 3) for k, v in want.items()}
    n = max(1, len(want))
    return (round(1 - sum(cers.values()) / n, 3) if isinstance(obj, dict) else 0.0), {
        "answered": bool((content or "").strip()), "parsed": isinstance(obj, dict), "cer": cers,
        "exact": round(sum(1 for c in cers.values() if c == 0) / n, 3)}


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
    if sc in ("counts", "labels", "text") and not case.get("labelled"):
        return None, {"answered": bool((content or "").strip()), "unlabelled": True}      # nothing to score against yet
    if sc == "counts":
        return score_counts(case, content)
    if sc == "labels":
        return score_labels(case, content)
    if sc == "text":
        return score_text(case, content)
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


# ---- images and the privacy guard (ADR-0134) ---------------------------------------------------------------------
IN_HOUSE = {}            # model → (task, None) once the gateway confirmed an in-house route, or (None, reason)
PROBE = [{"role": "user", "content": "Reply with OK."}]   # carries no case data


def in_house_route(model):
    """Does the gateway serve this model in-house, and does it enforce require_in_house? Asked once per model with a
    text-only probe. Only the gateway can answer: it matches a model name across every tenant's provider records and
    the first match wins, while this engine sees only its own tenant's records. In-house = the operator's own
    provider record marked hosting: in_house (a tenant's record cannot make an endpoint in-house). A gateway that
    ignores the flag answers without `hosting`, and then nothing private is sent."""
    if model not in IN_HOUSE:
        why = "not served by an in-house provider"
        for task in TASKS.get(model, ("chat", "eval")):
            try:
                r = call("/v1/complete", {"messages": PROBE, "max_tokens": 5, "model": model, "task": task,
                                          "receipt": False, "require_in_house": True}, timeout=60)
            except urllib.error.HTTPError as e:
                if e.code in (403, 501):                  # refused or not served for this task — try the next
                    continue
                why = "the in-house check failed (HTTP %s)" % e.code
                break
            except Exception as e:
                why = "the in-house check failed (%s)" % type(e).__name__
                break
            if r.get("hosting") == "in_house":
                IN_HOUSE[model] = (task, None)
            else:
                why = "the model gateway does not enforce in-house routing"
            break
        IN_HOUSE.setdefault(model, (None, why))
    return IN_HOUSE[model]


def is_private(c):
    """Cases with camera media are private unless marked otherwise; text cases only when marked private."""
    return (c.get("visibility") or "private") == "private" if c.get("inputs") else c.get("visibility") == "private"


def refuse_private(c, model):
    """A private case is never sent to a model that is not in-house. Returns the refusal, or None."""
    if not is_private(c):
        return None
    task, why = in_house_route(model)
    if task:
        return None
    return "refused: private case%s — %s: %s" % (" (camera media)" if c.get("inputs") else "", model, why)


def _mime(raw):
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def with_images(c):
    """The case's messages with its image inputs attached to the first user message as OpenAI-style image parts
    (data URLs). The gateway passes them to OpenAI-compatible providers as they are and converts them for Anthropic.
    The engine reads each file through the broker, so the gateway does no file access."""
    imgs = [i for i in (c.get("inputs") or []) if i.get("kind") == "image" and i.get("file_id")]
    msgs = [dict(m) for m in c.get("messages") or []]
    if not imgs:
        return msgs
    parts = []
    for i in imgs:
        raw = base64.b64decode((call("/v1/file/get", {"id": i["file_id"]}, timeout=90) or {}).get("content_b64") or "")
        mime = _mime(raw)
        if not mime:
            raise ValueError("input %s is not a JPEG, PNG or WebP image" % i["file_id"][:24])
        parts.append({"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())}})
    k = next((n for n, m in enumerate(msgs) if m.get("role") == "user"), None)
    if k is None:
        msgs.append({"role": "user", "content": parts}); return msgs
    text = msgs[k].get("content")
    msgs[k]["content"] = ([{"type": "text", "text": text}] if isinstance(text, str) else list(text or [])) + parts
    return msgs


def ask(model, c):
    """One case to one model through the broker → (output, served_by, usage, error, ms)."""
    t0 = time.time(); out = ""; served = None; usage = None; err = None
    why = refuse_private(c, model)
    if why:                                               # ADR-0132 §5 / ADR-0134: never sent, recorded as refused
        return "", None, None, why, 0
    try:
        res = None
        msgs = with_images(c)
        private = is_private(c)                           # the gateway re-checks the route on every private call
        tasks = (in_house_route(model)[0],) if private else TASKS.get(model, ("chat", "eval"))
        for task in tasks:                                # 'eval' = models registered for evaluation only —
            try:                                          # never on a production route (a call naming no model)
                res = call("/v1/complete", {"messages": msgs, "max_tokens": budget(c), "model": model, "task": task,
                                            "receipt": False, **({"require_in_house": True} if private else {}),
                                            **thinking_arg()}, timeout=240)
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
                          "score": (round(sum(float(r["score"]) for r in scored) / len(scored), 3) if (scored := [r for r in rs if r.get("score") is not None]) else None),
                          "scored": len([r for r in rs if r.get("score") is not None]),
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
        if how == "rescored":                       # keep what the candidate recorded (a detector's boxes, threshold, …)
            met = dict({k: v for k, v in (r.get("metrics") or {}).items() if k not in ("rescored",)}, **met)
        rec = result_record(run, rslug, r["model"], c, out, served, usage, err, ms, sc, met, rescored={"at": now(), "how": how, "was": r.get("score")})
        write("intelligence.eval_result", rec); new.append(rec); redone += 1
    summary = summarise(run, new)
    write("intelligence.eval_run", dict(run, summary=summary, note=((run.get("note") or "") + " · rescored " + now())[:500]))
    print("rescored %s: %d results (%d re-asked, %d cut answers kept as scored)" % (run_id, redone, rerun, kept), flush=True)
    print("RESULT: " + json.dumps({"run": run_id, "summary": summary}))
    return 0


def is_component(model):
    """A run subject answered by its own engine (e.g. component:uvh26-rtdetrv2-s → engine/vision-detect), not the gateway."""
    return str(model).startswith("component:")


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
    todo = [(m, c) for m in run.get("models") or [] if not is_component(m) for c in cases if (m, c["id"]) not in have]
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


# ---- seed an image suite from a Files folder (ADR-0134) -----------------------------------------------------------
_VIEW = re.compile(r"[\s_-]*((?:box|cam)\s*\d+|ptz\s*\d*)$", re.I)


def site_and_view(name):
    """A camera frame's junction and view from its file name: 'BANNS_MANDI_CHAURAHA_BOX2.jpg' → ('banns-mandi-chauraha',
    'box'); a name without a view token is its own site, view 'other'."""
    stem = re.sub(r"\.[a-z0-9]+$", "", name or "", flags=re.I)
    m = _VIEW.search(stem)
    view = re.sub(r"\d+", "", m.group(1)).strip().lower() if m else "other"
    return slug(stem[:m.start()] if m else stem, 60), view


def split_of(site, dev_share):
    """Development or held-back test split, by site: stable across runs, and every view of a junction lands together."""
    import hashlib
    return "dev" if int(hashlib.sha256(site.encode()).hexdigest()[:8], 16) % 1000 < dev_share * 1000 else "test"


def seed_images():
    """Build (or extend) an image suite from one Files folder: one private case per image per test in
    visual_tests.TESTS, tagged with its site, view and split. Existing cases — and their labels — are left as they are."""
    import visual_tests as vt
    folder = os.environ["SEED_FOLDER"].rstrip("/") or "/"
    suite = os.environ.get("SEED_SUITE") or "urban-visual-intelligence"
    only = {x.strip().upper() for x in (os.environ.get("SEED_TESTS") or "").split(",") if x.strip()}
    share = float(os.environ.get("SEED_DEV_SHARE") or 0.33)
    files = [f for f in mine("platform.file") if f.get("kind") == "file" and (f.get("folder") or "/").rstrip("/") == folder.rstrip("/")
             and f.get("file_id") and (str(f.get("content_type") or "").startswith("image/") or re.search(r"\.(jpe?g|png|webp)$", f.get("name") or "", re.I))]
    existing = {c["id"]: c for c in mine("intelligence.eval_case") if c.get("suite") == suite}
    have = set(existing)
    refresh = os.environ.get("SEED_UPDATE") == "1"           # re-write prompt, scorer and form of UNLABELLED cases
    updated = 0
    tests = [t for t in vt.TESTS if not only or t["test_id"] in only]
    if os.environ.get("SEED_ONLY_ACTIVE") == "1":             # new tests only for images already in the working set
        active = {(c.get("inputs") or [{}])[0].get("file_id") for c in existing.values() if c.get("status") == "active"}
        files = [f for f in files if f["file_id"] in active]
    made = 0; ts = now()
    for f in sorted(files, key=lambda x: x.get("name") or ""):
        site, view = site_and_view(f.get("name"))
        for t in tests:
            cid = "ds:%s:intelligence/eval_case/%s--%s--%s" % (T, slug(suite, 40), t["test_id"].lower(), slug(f.get("name"), 70))
            if cid in have:
                old = existing[cid]
                if refresh and not old.get("labelled") and (old.get("messages") != [{"role": "user", "content": t["prompt"]}]
                                                            or old.get("label_form") != [t["field"]] or old.get("scorer") != t["scorer"]):
                    write("intelligence.eval_case", dict(old, messages=[{"role": "user", "content": t["prompt"]}],
                          label_form=[t["field"]], scorer=t["scorer"], updated_at=ts))
                    updated += 1
                continue
            write("intelligence.eval_case", {
                "schema": "intelligence.eval_case", "id": cid, "tenant": T, "suite": suite, "task": "extract",
                "test_id": t["test_id"], "title": "%s · %s" % (t["title"], f.get("name")),
                "messages": [{"role": "user", "content": t["prompt"]}], "max_tokens": 1200,
                "inputs": [{"kind": "image", "file_id": f["file_id"], "name": f.get("name")}],
                "scorer": t["scorer"], "label_form": [t["field"]], "expected": None, "labelled": False,
                "source": {"kind": "files-folder", "folder": folder, "file_record": f.get("id"), "label": f.get("name")},
                "tags": ["test:" + t["test_id"], "site:" + site, "view:" + view, "split:" + split_of(site, share)],
                "visibility": "private", "status": "active", "created_by": "engine/model-eval", "created_at": ts, "updated_at": ts})
            made += 1
    print("seeded %s from %s: %d images × %d tests → %d new cases, %d unlabelled cases refreshed" % (suite, folder, len(files), len(tests), made, updated), flush=True)
    print("RESULT: " + json.dumps({"suite": suite, "images": len(files), "tests": len(tests), "new_cases": made, "refreshed": updated}))
    return 0


def main():
    if not T:
        raise SystemExit("FATAL: no tenant for this run (WRITE_TENANT / TENANT)")
    if os.environ.get("SEED_DOC"):
        return seed()
    if os.environ.get("SEED_FOLDER"):
        return seed_images()
    if os.environ.get("RESCORE_RUN"):
        return rescore(os.environ["RESCORE_RUN"], os.environ.get("RERUN_TRUNCATED") == "1")
    pending = sorted([r for r in mine("intelligence.eval_run") if r.get("status") == "pending"
                      and any(not is_component(m) for m in r.get("models") or [])],        # a detector-only run is not ours
                     key=lambda r: str(r.get("requested_at") or ""))
    print("tenant=%s · %d pending run(s)" % (T, len(pending)), flush=True)
    if pending:
        run_one(pending[0])                     # one run per invocation; the schedule / the next Run takes the next
    return 0


if __name__ == "__main__":
    sys.exit(main())
