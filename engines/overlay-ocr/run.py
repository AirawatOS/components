"""OVERLAY-OCR — an in-house OCR candidate for Model Lab's on-screen text test, C05 (ADR-0134).

For a run that lists `component:overlay-ocr` among its models (OCR_RUN, or the oldest pending such run), this engine
reads each camera frame's overlay — the date-time stamp and the camera or direction name burned into the picture — with
Tesseract on the platform's own CPU (runtime profile `ocr`); frames never leave the platform. It looks only at the top
and bottom strips, where camera overlays sit, enlarges them, reads them twice (plain greyscale, and white text isolated
as black on white), keeps confident words, then finds the timestamp with a pattern that tolerates OCR's usual letter /
digit mix-ups and the camera name from a "From …" line or a short confident line. The answer is written in the same
JSON a model gives ({"timestamp", "camera_name"}) with every recognised line kept, so engine/model-eval scores it
(RESCORE_RUN) with the text scorer (character error rate, exact match).
"""
import base64, io, json, os, re, sys, time, urllib.error, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ.get("RUN_TOKEN", "")
T = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""
SUBJECT = "component:overlay-ocr"
TESTS = ("C05",)
BAND = float(os.environ.get("OCR_BAND") or 0.14)        # top and bottom strips, as a share of the frame height
MIN_CONF = float(os.environ.get("OCR_MIN_CONF") or 55)   # Tesseract word confidence (0–100) kept
NAME_CONF = float(os.environ.get("OCR_NAME_CONF") or 80) # a name line that is not "From …" must be at least this sure


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
def _lines_from(d, rebuild):
    """Tesseract word data → [(line text, mean confidence)]. With `rebuild`, words are grouped into lines by their
    position (sparse mode reports each word as its own line, which split stamps like "10-1" + "O26 Fri 16:07:42")."""
    words = [(d["left"][i], d["top"][i], d["height"][i], w, float(d["conf"][i])) for i, w in enumerate(d["text"])
             if w.strip() and float(d["conf"][i]) >= MIN_CONF]
    if not rebuild:
        rows = {}
        for i, w in enumerate(d["text"]):
            c = float(d["conf"][i])
            if w.strip() and c >= MIN_CONF:
                rows.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append((d["left"][i], w, c))
        groups = [sorted(v) for v in rows.values()]
        return [(" ".join(x[1] for x in g), round(sum(x[2] for x in g) / len(g), 1)) for g in groups]
    lines = []                                                  # [[centre y, height, [(left, word, conf)]]]
    for left, top, hgt, w, c in sorted(words, key=lambda x: x[1] + x[2] / 2):
        cy = top + hgt / 2
        for ln in lines:
            if abs(ln[0] - cy) <= 0.6 * max(ln[1], hgt):
                ln[2].append((left, w, c)); break
        else:
            lines.append([cy, hgt, [(left, w, c)]])
    out = []
    for _cy, _h, ws in lines:
        ws.sort()
        out.append((" ".join(x[1] for x in ws), round(sum(x[2] for x in ws) / len(ws), 1)))
    return out


def read_lines(raw):
    """Confident text lines from the frame's top and bottom strips, each as (text, mean word confidence). Each strip is
    read three ways (greyscale; white text isolated at two thresholds) in two modes (block; sparse with lines rebuilt)."""
    import numpy as np
    import pytesseract
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(raw)).convert("L")
    w, h = im.size
    out, seen = [], set()
    for top, bot in ((0, BAND), (1 - BAND, 1)):
        band = im.crop((0, int(h * top), w, int(h * bot)))
        band = band.resize((band.width * 2, band.height * 2), Image.LANCZOS)
        a = np.asarray(band)
        variants = [ImageOps.autocontrast(band)] + [Image.fromarray(np.where(a >= t, 0, 255).astype("uint8")) for t in (200, 160)]
        for img in variants:
            for psm, rebuild in (("6", False), ("11", True)):
                d = pytesseract.image_to_data(img, config="--psm " + psm, output_type=pytesseract.Output.DICT)
                for line in _lines_from(d, rebuild):
                    if line[0] not in seen:
                        seen.add(line[0]); out.append(line)
    return out


def answer(lines):
    text = "\n".join(t for t, _c in lines)
    joined = " ".join(t for t, _c in lines)
    return {"timestamp": find_timestamp(text) or find_timestamp(joined), "camera_name": find_name(lines)}


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
            "served_by": "overlay-ocr", "output": "" if err else json.dumps(ans), "score": None, "latency_ms": ms, "error": err, "at": now(),
            "metrics": {"answered": not err, "lines": lines[:40], "band": BAND, "min_conf": MIN_CONF, "name_conf": NAME_CONF},
            "usage": {"hardware": "cpu", "device": "cpu", "model": "Tesseract 5 (eng), overlay strips"}})
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
