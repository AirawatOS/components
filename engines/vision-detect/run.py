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
THRESHOLD = float(os.environ.get("DETECT_THRESHOLD") or 0.5)
FLOOR = min(THRESHOLD, 0.25)            # detections down to here are KEPT (a threshold can be studied from one run)
THREADS = int(os.environ.get("DETECT_THREADS") or 4)
MODELS = {
    "uvh26": ("/opt/rtdetrv2/configs/rtdetrv2/uvh26_rtdetrv2_s.yml", "/opt/models/uvh26/rtdetrv2_s.pth"),
    "coco": ("/opt/rtdetrv2/configs/rtdetrv2/coco_rtdetrv2_s.yml", "/opt/models/coco/rtdetrv2_s.pth"),
    "coco_l": ("/opt/rtdetrv2/configs/rtdetrv2/coco_o365_rtdetrv2_l.yml", "/opt/models/coco/rtdetrv2_l_o365.pth"),
}
PEOPLE_MODELS = ("coco", "coco_l")      # COCO-class detectors (same 80 classes, same index order)
MODEL_NAMES = {"uvh26": "RT-DETRv2-S (UVH-26)", "coco": "RT-DETRv2-S (COCO)", "coco_l": "RT-DETRv2-L (COCO from Objects365)"}
# Subjects this engine answers, and with which models: UVH-26 alone (vehicles), or UVH-26 + COCO (+ pedestrians, animals).
SUBJECTS = {
    "component:uvh26-rtdetrv2-s": {"models": ["uvh26"], "tests": ("B01", "B03")},
    "component:uvh26+coco-rtdetrv2-s": {"models": ["uvh26", "coco"], "tests": ("B01", "B03", "B04", "B09")},
    "component:uvh26+o365-rtdetrv2-l": {"models": ["uvh26", "coco_l"], "tests": ("B01", "B03", "B04", "B09")},
}

# UVH-26 category ids (the released checkpoint's labels) → the suite's vehicle classes (visual_tests.VEHICLES).
UVH = {1: "Hatchback", 2: "Sedan", 3: "SUV", 4: "MUV", 5: "Bus", 6: "Truck", 7: "Three-wheeler", 8: "Two-wheeler",
       9: "LCV", 10: "Mini-bus", 11: "Tempo-traveller", 12: "Bicycle", 13: "Van", 14: "Others"}
TO_CLASS = {1: "car", 2: "car", 3: "car", 4: "car", 13: "car", 5: "bus", 10: "bus", 11: "bus", 6: "truck", 9: "truck",
            7: "three_wheeler", 8: "two_wheeler", 12: "bicycle"}            # 14 Others → not counted
VEHICLES = ["car", "two_wheeler", "three_wheeler", "bus", "truck", "bicycle"]
ANIMALS = ["cattle", "dog", "other_animal"]
# COCO class INDEX (0–79, as RT-DETRv2's post-processor returns it in deploy mode — it maps index → COCO category id only
# outside deploy mode) → the suite's classes, for what UVH-26 does not see: 0 person, 15 cat, 16 dog, 17 horse, 18 sheep,
# 19 cow, 20 elephant. COCO's own vehicles are ignored (UVH-26 counts those); birds (14) are not counted. UVH-26 was trained
# without COCO remapping, so its raw index IS its category id (1–14).
COCO = {0: "pedestrian", 19: "cattle", 16: "dog", 15: "other_animal", 17: "other_animal", 18: "other_animal", 20: "other_animal"}
THRESHOLD_PEOPLE = float(os.environ.get("DETECT_THRESHOLD_PEOPLE") or 0) or None   # optional own threshold for pedestrians and animals


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


def answer(test_id, dets, sees=("vehicles",)):
    """The pipeline's answer to one test, from detections at or above THRESHOLD (riders excluded), in the form the
    test's prompt asks a model for. `sees` = what the pipeline can detect, so B01 only claims classes it could see."""
    n = {}
    for d in dets:
        if d["score"] >= threshold_for(d) and not d.get("rider"):
            n[d["class"]] = n.get(d["class"], 0) + 1
    if test_id == "B03":
        return {"counts": {k: n.get(k, 0) for k in VEHICLES}}
    if test_id == "B04":
        return {"counts": {"pedestrian": n.get("pedestrian", 0)}}
    if test_id == "B09":
        return {"counts": {k: n.get(k, 0) for k in ANIMALS}}
    classes = VEHICLES + (["pedestrian"] + ANIMALS if "people_animals" in sees else [])       # B01
    present = [k for k in classes if n.get(k)]
    return {"labels": present or ["none"]}


def threshold_for(d):
    """The threshold a detection is counted at: THRESHOLD, or DETECT_THRESHOLD_PEOPLE for COCO's people and animals."""
    return THRESHOLD_PEOPLE if (THRESHOLD_PEOPLE and d.get("model") in PEOPLE_MODELS) else THRESHOLD


def _inside(a, b):
    """Share of box a's area that lies inside box b (boxes as [x1, y1, x2, y2])."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return ix * iy / area


def mark_riders(dets):
    """A COCO 'person' riding a detected two-wheeler or bicycle, or inside a car or three-wheeler, is not a pedestrian
    (B04's definition): a person mostly inside the vehicle's box stretched upward by its own height is marked
    rider=True and not counted — kept, so the rule can be checked."""
    rides = [d["box"] for d in dets if d["class"] in ("two_wheeler", "bicycle") and d["score"] >= THRESHOLD]      # counted vehicles only —
    holds = [d["box"] for d in dets if d["class"] in ("car", "three_wheeler", "bus", "truck") and d["score"] >= THRESHOLD]  # not phantoms
    for d in dets:
        if d["class"] != "pedestrian":
            continue
        up = [[b[0], b[1] - (b[3] - b[1]), b[2], b[3]] for b in rides]
        if any(_inside(d["box"], b) >= 0.6 for b in up) or any(_inside(d["box"], b) >= 0.7 for b in holds):
            d["rider"] = True
    return dets


class Detector:
    def __init__(self, names):
        import torch
        import torchvision.transforms as TT
        from src.core import YAMLConfig
        torch.set_num_threads(THREADS)
        self.torch, self.nets = torch, {}
        for name in names:
            cfgp, weights = MODELS[name]
            cfg = YAMLConfig(cfgp)
            ck = torch.load(weights, map_location="cpu", weights_only=False)
            cfg.model.load_state_dict(ck["ema"]["module"] if "ema" in ck else ck["model"], strict=True)
            self.nets[name] = (cfg.model.deploy().eval(), cfg.postprocessor.deploy())
        self.tf = TT.Compose([TT.Resize((640, 640)), TT.ToTensor()])          # as UVH-26's own inference script

    def _run(self, name, im, ox=0, oy=0):
        """One pass of one model over an image (or a crop at offset ox, oy) → raw (label, box in frame pixels, score)."""
        model, post = self.nets[name]; w, h = im.size
        labels, boxes, scores = post(model(self.tf(im)[None]), self.torch.tensor([[w, h]]))
        return [(int(l), [b[0] + ox, b[1] + oy, b[2] + ox, b[3] + oy], s)
                for l, b, s in zip(labels[0].tolist(), boxes[0].tolist(), scores[0].tolist()) if s >= FLOOR]

    def _tiles(self, w, h):
        """The full frame plus a 2 × 2 grid of quarters overlapping by ~20%: people and animals far from the camera are a
        few pixels tall once a 2048-px frame is shrunk to 640, and each quarter is seen ~2× closer (standard tiling)."""
        tw, th = int(w * 0.6), int(h * 0.6)
        return [(0, 0, w, h)] + [(x, y, x + tw, y + th) for x in (0, w - tw) for y in (0, h - th)]

    def detect(self, raw):
        from PIL import Image
        from torchvision.ops import batched_nms
        im = Image.open(io.BytesIO(raw)).convert("RGB"); w, h = im.size
        out = []
        with self.torch.no_grad():
            if "uvh26" in self.nets:                                       # vehicles: the full frame, as UVH-26 was trained
                for lab, box, sc in self._run("uvh26", im):
                    if lab in UVH:
                        out.append({"model": "uvh26", "uvh": UVH[lab], "class": TO_CLASS.get(lab, "other"), "score": round(sc, 3), "box": [round(v, 1) for v in box]})
            for name in [n for n in self.nets if n in PEOPLE_MODELS]:      # people and animals: full frame + tiles, merged
                raw_dets = []
                for x1, y1, x2, y2 in self._tiles(w, h):
                    crop = im if (x1, y1, x2, y2) == (0, 0, w, h) else im.crop((x1, y1, x2, y2))
                    raw_dets += [d for d in self._run(name, crop, x1, y1) if d[0] in COCO]
                if raw_dets:
                    T_ = self.torch
                    # one group per SUITE class, so a dog the model also called a horse or a sheep (all animals here) is
                    # one animal, not two: overlapping animal boxes merge across cattle / dog / other_animal
                    grp = T_.tensor([0 if COCO[d[0]] == "pedestrian" else 1 for d in raw_dets])
                    keep = batched_nms(T_.tensor([d[1] for d in raw_dets], dtype=T_.float32), T_.tensor([d[2] for d in raw_dets], dtype=T_.float32),
                                       grp, 0.5).tolist()
                    for k in keep:
                        lab, box, sc = raw_dets[k]
                        out.append({"model": name, "coco": lab, "class": COCO[lab], "score": round(sc, 3), "box": [round(v, 1) for v in box]})
        return mark_riders(out), (w, h)


def run_detector(run, subject):
    spec = SUBJECTS[subject]; sees = ("vehicles", "people_animals") if "coco" in spec["models"] else ("vehicles",)
    cases = [c for c in mine("intelligence.eval_case") if c.get("suite") == run["suite"] and c.get("status") == "active"
             and c.get("test_id") in spec["tests"] and (c.get("inputs") or [])]
    rslug = slug(run["id"].rsplit("/", 1)[-1], 40)
    have = {r.get("case_ref") for r in mine("intelligence.eval_result") if r.get("run_ref") == run["id"] and r.get("model") == subject}
    todo = [c for c in cases if c["id"] not in have]
    by_image = {}
    for c in todo:
        by_image.setdefault(c["inputs"][0]["file_id"], []).append(c)
    print("run %s: %d detector cases on %d images (%d already answered)" % (run["id"], len(todo), len(by_image), len(have)), flush=True)
    if not by_image:
        return 0
    det = Detector(spec["models"]); n = 0
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
            out = "" if err else json.dumps(answer(c["test_id"], dets, sees))
            write("intelligence.eval_result", {
                "schema": "intelligence.eval_result",
                "id": "ds:%s:intelligence/eval_result/%s--%s--%s" % (T, rslug, slug(subject, 30), slug(c["id"].rsplit("/", 1)[-1], 70)),
                "tenant": T, "run_ref": run["id"], "case_ref": c["id"], "suite": run["suite"], "model": subject,
                "served_by": subject.replace("component:", ""), "output": out, "score": None, "latency_ms": ms, "error": err, "at": now(),
                "metrics": {"answered": bool(out), "threshold": THRESHOLD, "threshold_people": THRESHOLD_PEOPLE, "floor": FLOOR, "image_size": size, "detections": dets[:400]},
                "usage": {"hardware": "cpu", "threads": THREADS, "device": "cpu",
                          "model": " + ".join(MODEL_NAMES[m] for m in spec["models"])}})
            n += 1
        print("  %s: %d objects ≥ %.2f in %d ms%s" % (cs[0]["inputs"][0].get("name") or fid[:20], sum(1 for d in dets if d["score"] >= threshold_for(d) and not d.get("rider")), THRESHOLD, ms,
                                                      " · " + err if err else ""), flush=True)
    if subject == [m for m in run.get("models") or [] if m in SUBJECTS][-1] and all(str(m).startswith("component:") for m in run.get("models") or []):
        write("intelligence.eval_run", dict(run, status="done", finished_at=now(), started_at=run.get("started_at") or now(),
              note=((run.get("note") or "") + " · detector answers in; score with Rescore").lstrip(" ·")[:500]))
    print("RESULT: " + json.dumps({"run": run["id"], "answers": n, "images": len(by_image)}))
    return 0


def main():
    if not T:
        raise SystemExit("FATAL: no tenant for this run (WRITE_TENANT / TENANT)")
    want = os.environ.get("DETECT_RUN")
    runs = [r for r in mine("intelligence.eval_run") if set(SUBJECTS) & set(r.get("models") or [])
            and (r["id"] == want if want else r.get("status") in ("pending", "running"))]
    runs.sort(key=lambda r: str(r.get("requested_at") or ""))
    if not runs:
        print("no run lists %s%s" % (" or ".join(SUBJECTS), " (DETECT_RUN=%s)" % want if want else "")); return 0
    for subject in [m for m in runs[0].get("models") or [] if m in SUBJECTS]:
        run_detector(runs[0], subject)
    return 0


if __name__ == "__main__":
    sys.exit(main())
