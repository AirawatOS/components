"""IMAGE-QUALITY-CV — a classical computer-vision candidate for Model Lab's image tests (ADR-0134).

For a run that lists `component:image-quality-cv` among its models (IQ_RUN, or the oldest pending such run), this
engine answers A01 (image quality), A02 (lighting and visibility) and A03 (blur) for every active image case by MEASURING
the pixels — no model, no network: sharpness (variance of the Laplacian), exposure (luminance percentiles, dark and
blown-out fractions), colour (saturation: night-time IR cameras turn grey), glare (the largest saturated patch), haze
(dark-channel brightness with low contrast) and broken frames (blank or undecodable). It turns the measures into the
same JSON a vision-language model gives, so engine/model-eval scores it (RESCORE_RUN) with the same scorers, and it
keeps every measure in the result so thresholds can be re-fitted later from stored results without reading images again.
Images are read through the broker and never leave the platform. Rain is never claimed: a still frame shows it too
weakly for pixel statistics (left to other candidates).

Thresholds are environment settings with defaults; fit them on the DEVELOPMENT split only (ADR-0134 §4).
"""
import base64, io, json, os, re, sys, time, urllib.error, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ.get("RUN_TOKEN", "")
T = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""
SUBJECT = "component:image-quality-cv"
TESTS = ("A01", "A02", "A03")


def _f(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


# Thresholds (on a frame scaled to 960 px wide, luminance 0–255). Defaults are a first guess; see the module docstring.
SHARP = _f("IQ_SHARP", 120)          # Laplacian variance at or above → sharp
BLURRED = _f("IQ_BLURRED", 40)       # below → blurred (between: slightly_blurred)
NIGHT_Y = _f("IQ_NIGHT_Y", 70)       # mean luminance below → night
IR_SAT = _f("IQ_IR_SAT", 0.06)       # mean saturation below, with mean luminance under IQ_IR_Y → night (IR mono)
IR_Y = _f("IQ_IR_Y", 120)
LOW_Y = _f("IQ_LOW_Y", 95)           # mean luminance below → low_light
GLARE_BLOB = _f("IQ_GLARE_BLOB", 0.006)   # largest near-white patch, as a share of the frame → glare
HAZE_DARK = _f("IQ_HAZE_DARK", 105)  # dark-channel mean above …
HAZE_STD = _f("IQ_HAZE_STD", 42)     # … with luminance spread below → haze
BLANK_STD = _f("IQ_BLANK_STD", 6)    # luminance spread below → a blank frame (poor)


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
    """Every record of `scope` in this tenant, following the broker's pages."""
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


# ---- measuring --------------------------------------------------------------------------------------------------
def measure(raw):
    """The pixel measures of one frame. Camera overlays (timestamp and name strips) sit in the top and bottom bands, so
    those bands are left out of every measure."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = im.size
    if w > 960:
        im = im.resize((960, max(1, round(h * 960 / w))), Image.BILINEAR)
    a = np.asarray(im).astype(np.float32)
    H = a.shape[0]; a = a[int(H * 0.08): max(int(H * 0.08) + 1, int(H * 0.94))]       # drop overlay bands
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    mx, mn = a.max(axis=2), a.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)
    lap = ndimage.laplace(y)
    white = y >= 250
    lab, n = ndimage.label(white)
    blob = (np.bincount(lab.ravel())[1:].max() / white.size) if n else 0.0
    dark = ndimage.minimum_filter(mn, size=15)                                        # the dark channel (haze prior)
    return {"width": w, "height": h, "lap_var": round(float(lap.var()), 2), "y_mean": round(float(y.mean()), 2),
            "y_std": round(float(y.std()), 2), "y_p05": round(float(np.percentile(y, 5)), 1), "y_p95": round(float(np.percentile(y, 95)), 1),
            "dark_frac": round(float((y < 40).mean()), 4), "bright_frac": round(float(white.mean()), 4),
            "sat_mean": round(float(sat.mean()), 4), "glare_blob": round(float(blob), 5), "dark_channel": round(float(dark.mean()), 2)}


def judge(m):
    """Measures → the three answers, in the same form as a model's."""
    if m is None:
        return {"A01": {"label": "poor"}, "A02": {"labels": []}, "A03": {"label": "blurred"}}
    blank = m["y_std"] < BLANK_STD
    sharp = "sharp" if m["lap_var"] >= SHARP else "blurred" if m["lap_var"] < BLURRED else "slightly_blurred"
    night = m["y_mean"] < NIGHT_Y or (m["sat_mean"] < IR_SAT and m["y_mean"] < IR_Y)
    light = ["night" if night else "day"]
    if m["y_mean"] < LOW_Y:
        light.append("low_light")
    if m["glare_blob"] >= GLARE_BLOB:
        light.append("glare")
    if m["dark_channel"] > HAZE_DARK and m["y_std"] < HAZE_STD:
        light.append("haze")
    if blank or m["y_mean"] < 20 or m["bright_frac"] > 0.5 or (sharp == "blurred" and m["lap_var"] < BLURRED / 2):
        q = "poor"
    elif sharp != "sharp" or "low_light" in light or "glare" in light or "haze" in light:
        q = "marginal"
    else:
        q = "good"
    return {"A01": {"label": q}, "A02": {"labels": light}, "A03": {"label": sharp}}


THRESHOLDS = {"IQ_SHARP": SHARP, "IQ_BLURRED": BLURRED, "IQ_NIGHT_Y": NIGHT_Y, "IQ_IR_SAT": IR_SAT, "IQ_IR_Y": IR_Y, "IQ_LOW_Y": LOW_Y,
              "IQ_GLARE_BLOB": GLARE_BLOB, "IQ_HAZE_DARK": HAZE_DARK, "IQ_HAZE_STD": HAZE_STD, "IQ_BLANK_STD": BLANK_STD}


# ---- the run ----------------------------------------------------------------------------------------------------
def run_iq(run):
    cases = [c for c in mine("intelligence.eval_case") if c.get("suite") == run["suite"] and c.get("status") == "active"
             and c.get("test_id") in TESTS and (c.get("inputs") or [])]
    rslug = slug(run["id"].rsplit("/", 1)[-1], 40)
    have = {r.get("case_ref") for r in mine("intelligence.eval_result") if r.get("run_ref") == run["id"] and r.get("model") == SUBJECT}
    by_image = {}
    for c in cases:
        if c["id"] not in have:
            by_image.setdefault(c["inputs"][0]["file_id"], []).append(c)
    print("run %s: %d image-quality cases on %d images (%d already answered)" % (run["id"], sum(map(len, by_image.values())), len(by_image), len(have)), flush=True)
    n = 0
    for fid, cs in by_image.items():
        err = None; m = None; t0 = time.time()
        try:
            raw = base64.b64decode((call("/v1/file/get", {"id": fid}, timeout=90) or {}).get("content_b64") or "")
            m = measure(raw)
        except Exception as e:                    # an undecodable frame is itself an answer: poor quality
            err = "%s: %s" % (type(e).__name__, str(e)[:200])
        ms = int((time.time() - t0) * 1000)
        ans = judge(m)
        for c in cs:
            write("intelligence.eval_result", {
                "schema": "intelligence.eval_result",
                "id": "ds:%s:intelligence/eval_result/%s--%s--%s" % (T, rslug, slug(SUBJECT, 30), slug(c["id"].rsplit("/", 1)[-1], 70)),
                "tenant": T, "run_ref": run["id"], "case_ref": c["id"], "suite": run["suite"], "model": SUBJECT,
                "served_by": "image-quality-cv", "output": json.dumps(ans[c["test_id"]]), "score": None, "latency_ms": ms,
                "error": None, "at": now(),
                "metrics": {"answered": True, "measures": m, "decode_error": err, "thresholds": THRESHOLDS},
                "usage": {"hardware": "cpu", "device": "cpu", "model": "classical CV (numpy + scipy), no model weights"}})
            n += 1
        print("  %s: %s · %s · %s in %d ms%s" % (cs[0]["inputs"][0].get("name") or fid[:20], ans["A01"]["label"], ",".join(ans["A02"]["labels"]),
                                                ans["A03"]["label"], ms, " · " + err if err else ""), flush=True)
    if all(str(x).startswith("component:") for x in run.get("models") or []):
        write("intelligence.eval_run", dict(run, status="done", finished_at=now(), started_at=run.get("started_at") or now(),
              note=((run.get("note") or "") + " · image-quality answers in; score with Rescore").lstrip(" ·")[:500]))
    print("RESULT: " + json.dumps({"run": run["id"], "answers": n, "images": len(by_image)}))


def main():
    if not T:
        raise SystemExit("FATAL: no tenant for this run (WRITE_TENANT / TENANT)")
    want = os.environ.get("IQ_RUN")
    runs = [r for r in mine("intelligence.eval_run") if SUBJECT in (r.get("models") or [])
            and (r["id"] == want if want else r.get("status") in ("pending", "running"))]
    runs.sort(key=lambda r: str(r.get("requested_at") or ""))
    if not runs:
        print("no run lists %s%s" % (SUBJECT, " (IQ_RUN=%s)" % want if want else "")); return 0
    run_iq(runs[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
