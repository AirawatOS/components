"""OVERLAY-PPOCR — PP-OCRv5 as a Model Lab candidate for on-screen text, C05 (ADR-0134).

For a run that lists `component:overlay-ppocrv5` (OCR_RUN, or the oldest pending such run), this engine reads each camera
frame's overlay — the date-time stamp and the camera or direction name — with PaddleOCR's PP-OCRv5 (server detection and
recognition) on the platform's own CPU (runtime profile `paddleocr`); frames never leave the platform. PP-OCR finds text
lines itself, so it reads the top and bottom strips where camera overlays sit, and the whole frame only when no stamp is
found there. Timestamp and name come from the same rules as engine/overlay-ocr (tested there), so the two OCR engines
differ only in how they read. Answers are written in the same JSON a model gives, with every line and its score kept;
engine/model-eval scores the run (RESCORE_RUN) with the text scorer.
"""
import base64, io, json, os, re, sys, time, urllib.error, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ.get("RUN_TOKEN", "")
T = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""
SUBJECT = "component:overlay-ppocrv5"
TESTS = ("C05",)
BAND = float(os.environ.get("OCR_BAND") or 0.15)
MIN_CONF = float(os.environ.get("OCR_MIN_CONF") or 50)     # PP-OCR line score (0–1) × 100 kept
NAME_CONF = float(os.environ.get("OCR_NAME_CONF") or 85)
MODELS = {"text_detection_model_name": os.environ.get("PPOCR_DET") or "PP-OCRv5_server_det",
          "text_recognition_model_name": os.environ.get("PPOCR_REC") or "PP-OCRv5_server_rec"}


def call(path, body, timeout=120):
    for a in range(4):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Run-Token": TOKEN})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code >= 500 and a < 3:
                time.sleep(2 * (a + 1)); continue
            raise
        except urllib.error.URLError:
            if a < 3:
                time.sleep(2 * (a + 1)); continue
            raise


def mine(scope, limit=20000):
    rows, cur = [], None
    while len(rows) < limit:
        body = {"scope": scope, "limit": min(5000, limit - len(rows))}
        if cur:
            body["cursor"] = cur
        d = call("/v1/query", body)
        rows += [r.get("object") or r for r in (d.get("results") or [])]
        cur = d.get("next_cursor")
        if not cur:
            break
    return [o for o in rows if str(o.get("id", "")).startswith("ds:%s:" % T)]


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write(scope, obj):
    t = now(); obj = {k: v for k, v in obj.items() if v is not None}
    call("/v1/write", {"scope": scope, "id": obj["id"], "object": obj, "valid_time": {"from": t}, "asserted_at": t})


def slug(s, n=60):
    return (re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:n]) or "x"


# ---- parsing (pure text; tested without Tesseract) --------------------------------------------------------------
_D = r"[0-9OoIl|SB]"                                       # a digit as OCR may misread it
TS = re.compile(r"(%s{2})\s*[-/.]\s*(%s{2})\s*[-/.]\s*(%s{4})\s*([A-Za-z]{3})\s*(%s{2})\s*[:.]\s*(%s{2})\s*[:.]\s*(%s{2})" % ((_D,) * 6))
_FIX = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1", "S": "5", "B": "8"})
DAYS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}


def find_timestamp(text):
    """The overlay's date-time stamp, as printed (field order kept: some cameras print day first)."""
    m = TS.search(text or "")
    if not m:
        return ""
    a, b, y, day, hh, mm, ss = m.groups()
    a, b, y, hh, mm, ss = (x.translate(_FIX) for x in (a, b, y, hh, mm, ss))
    day = DAYS.get(day.lower(), day[:1].upper() + day[1:].lower())
    return "%s-%s-%s %s %s:%s:%s" % (a, b, y, day, hh, mm, ss)


def find_name(lines):
    """The camera / direction name: a "From …" line first; else a short, confident line that is not the timestamp."""
    best = None
    for text, conf in lines:
        t = re.sub(r"\s+", " ", text).strip(" .,:;-_|")
        if not t or find_timestamp(t):
            continue
        m = re.search(r"\b(from)\b\s+(.+)", t, re.I)
        if m:
            name = m.group(1) + " " + re.sub(r"[^A-Za-z0-9 _-]", "", m.group(2)).strip()
            return name if len(name) > 5 else ""
        if t.lower() == "from" or re.search(r"\d{1,4}\s*-\s*\d|\b(mon|tue|wed|thu|fri|sat|sun)\b", t, re.I):
            continue                                           # a piece of the timestamp, not a name
        if conf >= NAME_CONF and 2 <= len(t) <= 24 and re.fullmatch(r"[A-Za-z0-9 _-]+", t) and sum(ch.isalnum() for ch in t) >= 3:
            if best is None or conf > best[1]:
                best = (t, conf)
    return best[0] if best else ""


# ---- reading ----------------------------------------------------------------------------------------------------
_OCR = None


def _ocr():
    global _OCR
    if _OCR is None:
        from paddleocr import PaddleOCR
        _OCR = PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, **MODELS)
    return _OCR


def read_lines(raw):
    """(text, confidence 0–100) lines from ONE read of the whole frame (PP-OCR scales a short strip up to 736 px high,
    which distorted wide strips): lines whose centre lies in the top or bottom strip first, where camera overlays sit;
    the rest only if no stamp is found there."""
    import numpy as np
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    h = im.size[1]
    band, rest = [], []
    for r in _ocr().predict(np.asarray(im)):
        for t, sc, poly in zip(r["rec_texts"], r["rec_scores"], r["rec_polys"]):
            if not t.strip() or sc * 100 < MIN_CONF:
                continue
            cy = float(np.asarray(poly)[:, 1].mean()) / h
            (band if cy <= BAND or cy >= 1 - BAND else rest).append((t.strip(), round(float(sc) * 100, 1)))
    return band if find_timestamp("\n".join(t for t, _c in band)) else band + rest


def answer(lines):
    text = "\n".join(t for t, _c in lines)
    return {"timestamp": find_timestamp(text) or find_timestamp(" ".join(t for t, _c in lines)), "camera_name": find_name(lines)}


# ---- the run ----------------------------------------------------------------------------------------------------
def run_ocr(run):
    cases = [c for c in mine("intelligence.eval_case") if c.get("suite") == run["suite"] and c.get("status") == "active"
             and c.get("test_id") in TESTS and (c.get("inputs") or [])]
    rslug = slug(run["id"].rsplit("/", 1)[-1], 40)
    have = {r.get("case_ref") for r in mine("intelligence.eval_result") if r.get("run_ref") == run["id"] and r.get("model") == SUBJECT}
    todo = [c for c in cases if c["id"] not in have]
    print("run %s: %d overlay cases (%d already answered)" % (run["id"], len(todo), len(have)), flush=True)
    n = 0
    for c in todo:
        err = None; lines = []; t0 = time.time()
        try:
            raw = base64.b64decode((call("/v1/file/get", {"id": c["inputs"][0]["file_id"]}, timeout=90) or {}).get("content_b64") or "")
            lines = read_lines(raw)
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, str(e)[:200])
        ms = int((time.time() - t0) * 1000)
        ans = answer(lines)
        write("intelligence.eval_result", {
            "schema": "intelligence.eval_result",
            "id": "ds:%s:intelligence/eval_result/%s--%s--%s" % (T, rslug, slug(SUBJECT, 30), slug(c["id"].rsplit("/", 1)[-1], 70)),
            "tenant": T, "run_ref": run["id"], "case_ref": c["id"], "suite": run["suite"], "model": SUBJECT,
            "served_by": "overlay-ppocrv5", "output": "" if err else json.dumps(ans), "score": None, "latency_ms": ms, "error": err, "at": now(),
            "metrics": {"answered": not err, "lines": lines[:60], "band": BAND, "min_conf": MIN_CONF, "name_conf": NAME_CONF, "models": MODELS},
            "usage": {"hardware": "cpu", "device": "cpu", "model": "PaddleOCR 3.0 " + MODELS["text_detection_model_name"] + " + " + MODELS["text_recognition_model_name"]}})
        n += 1
        print("  %s: %s | %s in %d ms%s" % (c["inputs"][0].get("name") or c["id"][-30:], ans["timestamp"] or "—", ans["camera_name"] or "—", ms,
                                          " · " + err if err else ""), flush=True)
    if all(str(x).startswith("component:") for x in run.get("models") or []):
        write("intelligence.eval_run", dict(run, status="done", finished_at=now(), started_at=run.get("started_at") or now(),
              note=((run.get("note") or "") + " · OCR answers in; score with Rescore").lstrip(" ·")[:500]))
    print("RESULT: " + json.dumps({"run": run["id"], "answers": n}))


def main():
    if not T:
        raise SystemExit("FATAL: no tenant for this run (WRITE_TENANT / TENANT)")
    want = os.environ.get("OCR_RUN")
    runs = [r for r in mine("intelligence.eval_run") if SUBJECT in (r.get("models") or [])
            and (r["id"] == want if want else r.get("status") in ("pending", "running"))]
    runs.sort(key=lambda r: str(r.get("requested_at") or ""))
    if not runs:
        print("no run lists %s%s" % (SUBJECT, " (OCR_RUN=%s)" % want if want else "")); return 0
    run_ocr(runs[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
