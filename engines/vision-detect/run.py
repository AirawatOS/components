#!/usr/bin/env python3
"""vision-detect — answers a Model Lab run's detector tests with an in-house detector (ADR-0134).

For a run that lists `component:uvh26-rtdetrv2-s` among its models (DETECT_RUN, or the oldest pending such run):
every active case of the run's suite whose test this detector answers (B01 classes present, B03 vehicles by class)
gets an eval_result — the answer in the JSON form a model would give, the raw detections in metrics, wall time per
image, and the hardware. Each image is detected once and answers every test it has. Scores are left to
engine/model-eval (RESCORE_RUN), so every candidate is scored by the same code. When the detector is the run's only
model, the run is marked done here. Confined — the only I/O is the broker; the model runs on this host's CPU.
"""
import base64, io, json, os, re, sys, time, urllib.error, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ.get("RUN_TOKEN", "")
T = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""
SUBJECT = "component:uvh26-rtdetrv2-s"
THRESHOLD = float(os.environ.get("DETECT_THRESHOLD") or 0.5)
THREADS = int(os.environ.get("DETECT_THREADS") or 4)
CONFIG = "/opt/rtdetrv2/configs/rtdetrv2/uvh26_rtdetrv2_s.yml"
WEIGHTS = "/opt/models/uvh26/rtdetrv2_s.pth"

# UVH-26 category ids (the released checkpoint's labels) → the suite's vehicle classes (visual_tests.VEHICLES).
UVH = {1: "Hatchback", 2: "Sedan", 3: "SUV", 4: "MUV", 5: "Bus", 6: "Truck", 7: "Three-wheeler", 8: "Two-wheeler",
       9: "LCV", 10: "Mini-bus", 11: "Tempo-traveller", 12: "Bicycle", 13: "Van", 14: "Others"}
TO_CLASS = {1: "car", 2: "car", 3: "car", 4: "car", 13: "car", 5: "bus", 10: "bus", 11: "bus", 6: "truck", 9: "truck",
            7: "three_wheeler", 8: "two_wheeler", 12: "bicycle"}            # 14 Others → not counted
VEHICLES = ["car", "two_wheeler", "three_wheeler", "bus", "truck", "bicycle"]
ANSWERS = ("B01", "B03")


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
    rows = [r.get("object") or r for r in (call("/v1/query", {"scope": scope, "limit": limit}).get("results") or [])]
    return [o for o in rows if str(o.get("id", "")).startswith("ds:%s:" % T)]


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write(scope, obj):
    t = now(); obj = {k: v for k, v in obj.items() if v is not None}
    call("/v1/write", {"scope": scope, "id": obj["id"], "object": obj, "valid_time": {"from": t}, "asserted_at": t})


def slug(s, n=60):
    return (re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:n]) or "x"


def answer(test_id, dets):
    """The detector's answer to one test, in the form the test's prompt asks a model for."""
    counts = {k: 0 for k in VEHICLES}
    for d in dets:
        if d["class"] in counts:
            counts[d["class"]] += 1
    if test_id == "B03":
        return {"counts": counts}
    present = [k for k in VEHICLES if counts[k]]                          # B01 (the detector sees no pedestrians)
    return {"labels": present or ["none"]}


class Detector:
    def __init__(self):
        import torch
        import torchvision.transforms as TT
        from src.core import YAMLConfig
        torch.set_num_threads(THREADS)
        cfg = YAMLConfig(CONFIG)
        ck = torch.load(WEIGHTS, map_location="cpu", weights_only=False)
        cfg.model.load_state_dict(ck["ema"]["module"] if "ema" in ck else ck["model"], strict=True)
        self.torch, self.model, self.post = torch, cfg.model.deploy().eval(), cfg.postprocessor.deploy()
        self.tf = TT.Compose([TT.Resize((640, 640)), TT.ToTensor()])          # as UVH-26's own inference script

    def detect(self, raw):
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB"); w, h = im.size
        with self.torch.no_grad():
            labels, boxes, scores = self.post(self.model(self.tf(im)[None]), self.torch.tensor([[w, h]]))
        out = []
        for lab, box, sc in zip(labels[0].tolist(), boxes[0].tolist(), scores[0].tolist()):
            if sc >= THRESHOLD and int(lab) in UVH:
                out.append({"uvh": UVH[int(lab)], "class": TO_CLASS.get(int(lab), "other"), "score": round(sc, 3),
                            "box": [round(v, 1) for v in box]})
        return out, (w, h)


def run_detector(run):
    cases = [c for c in mine("intelligence.eval_case") if c.get("suite") == run["suite"] and c.get("status") == "active"
             and c.get("test_id") in ANSWERS and (c.get("inputs") or [])]
    rslug = slug(run["id"].rsplit("/", 1)[-1], 40)
    have = {r.get("case_ref") for r in mine("intelligence.eval_result") if r.get("run_ref") == run["id"] and r.get("model") == SUBJECT}
    todo = [c for c in cases if c["id"] not in have]
    by_image = {}
    for c in todo:
        by_image.setdefault(c["inputs"][0]["file_id"], []).append(c)
    print("run %s: %d detector cases on %d images (%d already answered)" % (run["id"], len(todo), len(by_image), len(have)), flush=True)
    if not by_image:
        return 0
    det = Detector(); n = 0
    for fid, cs in by_image.items():
        err = None; dets = []; size = None
        t0 = time.time()
        try:
            raw = base64.b64decode((call("/v1/file/get", {"id": fid}, timeout=90) or {}).get("content_b64") or "")
            dets, size = det.detect(raw)
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, str(e)[:200])
        ms = int((time.time() - t0) * 1000)
        for c in cs:
            out = "" if err else json.dumps(answer(c["test_id"], dets))
            write("intelligence.eval_result", {
                "schema": "intelligence.eval_result",
                "id": "ds:%s:intelligence/eval_result/%s--%s--%s" % (T, rslug, slug(SUBJECT, 30), slug(c["id"].rsplit("/", 1)[-1], 70)),
                "tenant": T, "run_ref": run["id"], "case_ref": c["id"], "suite": run["suite"], "model": SUBJECT,
                "served_by": "uvh26-rtdetrv2-s", "output": out, "score": None, "latency_ms": ms, "error": err, "at": now(),
                "metrics": {"answered": bool(out), "threshold": THRESHOLD, "image_size": size, "detections": dets[:300]},
                "usage": {"hardware": "cpu", "threads": THREADS, "device": "cpu", "model": "RT-DETRv2-S (UVH-26)"}})
            n += 1
        print("  %s: %d objects ≥ %.2f in %d ms%s" % (cs[0]["inputs"][0].get("name") or fid[:20], len(dets), THRESHOLD, ms,
                                                      " · " + err if err else ""), flush=True)
    if all(str(m).startswith("component:") for m in run.get("models") or []):
        write("intelligence.eval_run", dict(run, status="done", finished_at=now(), started_at=run.get("started_at") or now(),
              note=((run.get("note") or "") + " · detector answers in; score with Rescore").lstrip(" ·")[:500]))
    print("RESULT: " + json.dumps({"run": run["id"], "answers": n, "images": len(by_image)}))
    return 0


def main():
    if not T:
        raise SystemExit("FATAL: no tenant for this run (WRITE_TENANT / TENANT)")
    want = os.environ.get("DETECT_RUN")
    runs = [r for r in mine("intelligence.eval_run") if SUBJECT in (r.get("models") or [])
            and (r["id"] == want if want else r.get("status") in ("pending", "running"))]
    runs.sort(key=lambda r: str(r.get("requested_at") or ""))
    if not runs:
        print("no run lists %s%s" % (SUBJECT, " (DETECT_RUN=%s)" % want if want else "")); return 0
    return run_detector(runs[0])


if __name__ == "__main__":
    sys.exit(main())
