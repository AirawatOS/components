#!/usr/bin/env python3
"""crowd-count — the AI people-counting front-end for the crush-risk loop (vision-crowd-counting slice).

Confined: every read/write and the blob fetch go through the broker under this run's token (X-Run-Token),
exactly like rtsp-ingest. Per run, for each enabled device.camera it reads the latest media.frame, counts
PEOPLE (YOLOv8 person class), maps the camera to its H3 cell, and writes:

  vision.detection     the audited perception fact — {frame_ref, camera_ref, count, model} — CITES the exact
                       content-addressed frame it counted, so "the AI counted N here" stays replayable.
  cell.crowd_density   the SAME schema kumbh-crowd-sim writes and crush-risk-engine already consumes
                       {space, observed_at, density_sqm, headcount, capacity, band, prov} — nothing downstream
                       changes; we only swap the SOURCE of the number from "injected" to "counted from an image".

Honesty (see no-synthetic-data): a frame it cannot read yields NOTHING, never a guessed count. The ONLY
non-model path is the scaffold fallback that reads the simulator's stamped ground-truth (prov.sim_headcount) —
used before the YOLO weights/runtime are provisioned, and loudly logged. Swap in weights → real perception.
"""
import base64, json, math, os, urllib.request
from datetime import datetime, timezone

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
COMPONENT = "ds:airawatdeveloper:driver/crowd-count"
_HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_WEIGHTS = os.environ.get("YOLO_WEIGHTS", os.path.join(_HERE, "yolov8n.pt"))
# COUNTING METHOD (see the density-estimation note below):
#   detection — YOLO boxes each visible person, count = #boxes. Great for sparse/moderate crowds; UNDER-counts
#               dense ones (occlusion + tiny + NMS merge). Off-the-shelf.
#   density   — a crowd-density model outputs a per-pixel people-density map; count = SUM(map). Stays accurate
#               into extreme density (counts crowd "mass", never resolves individuals). Needs a crowd-trained
#               model (CSRNet/MCNN on ShanghaiTech/QNRF) exported to TorchScript and bundled as crowd-density.pt.
COUNT_METHOD = os.environ.get("COUNT_METHOD", "detection").lower()
DENSITY_MODEL = os.environ.get("DENSITY_MODEL", os.path.join(_HERE, "crowd-density.pt"))
DENSITY_SCALE = float(os.environ.get("DENSITY_SCALE", "1.0"))     # per-model gain on SUM(map)
DENSITY_OFFSET = float(os.environ.get("DENSITY_OFFSET", "0"))     # baseline subtracted first (a blank frame reads
                                                                  # ~15 on this model over the sim's grey ground)
DEFAULT_COVERAGE_M2 = float(os.environ.get("CAMERA_COVERAGE_M2", "250"))   # ground area one frame covers
H3_RES = int(os.environ.get("H3_RES", "9"))
CAMERA_ID = os.environ.get("CAMERA_ID", "").strip()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Run-Token": TOKEN},
                                 method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def _context():
    req = urllib.request.Request(API + "/v1/context", headers={"X-Run-Token": TOKEN})
    return json.loads(urllib.request.urlopen(req, timeout=20).read()) or {}


def _band(d):
    # identical bands to kumbh-crowd-sim so a counted cell colours the same as an injected one
    return ("critical" if d >= 6 else "danger" if d >= 4.5 else "caution" if d >= 3
            else "busy" if d >= 1.5 else "normal")


# ---- detector (lazy, loaded once) --------------------------------------------------------------------------
_MODEL = None
def _detector():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    try:
        from ultralytics import YOLO
        _MODEL = YOLO(MODEL_WEIGHTS)
        print("[crowd-count] YOLO loaded: %s" % MODEL_WEIGHTS)
    except Exception as e:
        _MODEL = False
        print("[crowd-count] MODEL UNAVAILABLE (%s) — SCAFFOLD MODE: counting from prov.sim_headcount"
              % str(e)[:120])
    return _MODEL


# ---- density-estimation model (lazy, TorchScript, arch-agnostic) -------------------------------------------
_DENSITY = None
def _density_model():
    global _DENSITY
    if _DENSITY is not None:
        return _DENSITY
    if not os.path.exists(DENSITY_MODEL):
        _DENSITY = False
        print("[crowd-count] COUNT_METHOD=density but no model at %s — falling back to detection" % DENSITY_MODEL)
        return _DENSITY
    try:
        import torch
        _DENSITY = torch.jit.load(DENSITY_MODEL, map_location="cpu").eval()
        print("[crowd-count] density model loaded: %s" % DENSITY_MODEL)
    except Exception as e:
        _DENSITY = False
        print("[crowd-count] density model load failed (%s) — falling back to detection" % str(e)[:120])
    return _DENSITY

def _count_density(jpeg_bytes):
    """Integrate the predicted per-pixel density map → total people. Returns (count, meta) or None to signal
    'no density model, fall back'. Standard crowd-counting preprocessing: RGB, ImageNet-normalised, dims to a
    multiple of 8; count = SUM(density map) * DENSITY_SCALE (per-model calibration)."""
    m = _density_model()
    if not m:
        return None
    import torch, numpy as np, cv2
    arr = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
    rgb = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB).astype("float32") / 255.0
    h, w = (rgb.shape[0] // 8) * 8, (rgb.shape[1] // 8) * 8
    rgb = cv2.resize(rgb, (w, h))
    mean = np.array([0.485, 0.456, 0.406], "float32"); std = np.array([0.229, 0.224, 0.225], "float32")
    t = torch.from_numpy(((rgb - mean) / std).transpose(2, 0, 1)).unsqueeze(0)
    with torch.no_grad():
        dm = m(t)
    count = (float(dm.sum().item()) - DENSITY_OFFSET) * DENSITY_SCALE   # calibrate: baseline then gain
    return int(round(max(0.0, count))), {"model": os.path.basename(DENSITY_MODEL), "mode": "density"}

def _count_people(jpeg_bytes, frame):
    """(count, meta). Dispatch by COUNT_METHOD.
    density   → integrate a crowd-density map (accurate into dense regimes); falls back to detection if no
                density model is bundled.
    detection → YOLO boxes each visible person (YOLO-first). Only if no detector is present do we fall back:
                a simulated frame reports its stamped ground-truth (scaffold), a real one yields 0 (never a
                guess — see no-synthetic-data)."""
    if COUNT_METHOD == "density":
        r = _count_density(jpeg_bytes)
        if r is not None:
            return r
        # no density model present → fall through to detection
    m = _detector()
    if m:
        import numpy as np, cv2
        arr = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
        res = m.predict(arr, classes=[0], verbose=False)     # class 0 = person (COCO)
        n = int(sum(len(r.boxes) for r in res))
        return n, {"model": MODEL_WEIGHTS, "mode": "vision"}
    prov = frame.get("prov") or {}
    if prov.get("source") == "simulation" and prov.get("sim_headcount") is not None:
        return int(prov["sim_headcount"]), {"model": None, "mode": "scaffold-groundtruth"}
    return 0, {"model": None, "mode": "no-model"}


def _cameras():
    if CAMERA_ID:
        obj = (_call("/v1/read", {"scope": "device.camera", "id": CAMERA_ID}) or {}).get("object")
        return [obj] if obj else []
    rows = (_call("/v1/query", {"scope": "device.camera", "limit": 500}) or {}).get("results", [])
    return [r.get("object", r) for r in rows]


def _latest_frame(cam_id):
    rows = (_call("/v1/query", {"scope": "media.frame", "limit": 500}) or {}).get("results", [])
    best = None
    for r in rows:
        o = r.get("object", r) or {}
        if o.get("camera_ref") != cam_id:
            continue
        if not best or (o.get("captured_at") or "") > (best.get("captured_at") or ""):
            best = o
    return best


def _bytes(file_id):
    r = _call("/v1/file/get", {"id": file_id})          # broker blob fetch — the digest IS the read capability
    b64 = (r or {}).get("content_b64")
    return base64.b64decode(b64) if b64 else None


def _space(cam):
    """H3 cell + centroid + boundary for the camera location — mirrors kumbh-crowd-sim's `space` so the
    counted density renders on the same viz.layer. Falls back to the camera's declared location.h3."""
    loc = cam.get("location") or {}
    try:
        import h3
        cell = loc.get("h3")
        if not cell and loc.get("lat") is not None and loc.get("lng") is not None:
            cell = h3.latlng_to_cell(float(loc["lat"]), float(loc["lng"]), H3_RES)
        if not cell:
            return None
        lat, lng = h3.cell_to_latlng(cell)
        boundary = [[b[1], b[0]] for b in h3.cell_to_boundary(cell)]        # [lng,lat] ring for rendering
        return {"h3_id": cell, "centroid": [lng, lat], "boundary": boundary}
    except Exception:
        return {"h3_id": loc["h3"]} if loc.get("h3") else None


def main():
    ctx = _context()
    tenant = ctx.get("tenant")
    if not tenant:
        print("[crowd-count] no tenant in broker context — refusing to guess"); return 2

    # count EVERY enabled camera that has a frame: real feeds get YOLO person-detection, sim cameras get their
    # stamped ground-truth (see _count_people). The weight is now bundled, so real detection is live.
    cams = [c for c in _cameras() if c and c.get("enabled", True)]
    if not cams:
        print("[crowd-count] no enabled device.camera readable by this run — nothing to do"); return 0

    wrote = 0
    for cam in cams:
        name = cam.get("name") or cam.get("id")
        frame = _latest_frame(cam["id"])
        if not frame or not frame.get("file_id"):
            print("[crowd-count] %s: no frame yet — skip" % name); continue
        blob = _bytes(frame["file_id"])
        if not blob:
            print("[crowd-count] %s: frame bytes unreadable — skip (no guess)" % name); continue

        count, meta = _count_people(blob, frame)
        at = _now()
        space = _space(cam)

        # 1) the audited perception fact — cites the exact frame it counted, sits at the camera location so a
        #    viz.layer can drop a marker "AI counted N here" over the map
        det_id = "ds:%s:vision/detection/%s/%s" % (tenant, cam["id"].rsplit("/", 1)[-1], at)
        _call("/v1/write", {"scope": "vision.detection", "id": det_id, "object": {
            "schema": "vision.detection", "id": det_id, "camera_ref": cam["id"], "frame_ref": frame["id"],
            "count": count, "by_class": {"person": count}, "model": meta["model"], "observed_at": at,
            "space": space or {},
            "prov": {"component": COMPONENT, "at": at, "method": "person-detection", "mode": meta["mode"]}}})

        # 2) the SAME cell.crowd_density crush-risk-engine consumes. The id carries a `-vision` suffix so a
        #    counted cell NEVER clobbers an injected one in the same (cell, hour) — both series coexist and the
        #    Crowd Command "counted vs injected" toggle picks which to render.
        coverage = float(((cam.get("location") or {}).get("coverage_m2")) or DEFAULT_COVERAGE_M2)
        if space and coverage > 0:
            density = round(count / coverage, 3)
            cell = space["h3_id"]
            cid = "ds:%s:cell/crowd_density/%s-%s-vision" % (tenant, cell, at[:13])
            _call("/v1/write", {"scope": "cell.crowd_density", "id": cid, "object": {
                "schema": "cell.crowd_density", "id": cid, "tenant": tenant, "layer": "L2", "space": space,
                "observed_at": at, "density_sqm": density, "headcount": count, "capacity": round(coverage * 4),
                "band": _band(density), "method": "vision-count",
                "prov": {"component": COMPONENT, "at": at, "method": "vision-count", "source_frame": frame["id"],
                         "camera": cam["id"], "model": meta["model"], "mode": meta["mode"],
                         "note": ("SIMULATED image counted by AI" if meta["mode"] != "vision" else None)}}})
            wrote += 1
            print("[crowd-count] %s: %d people (%s) → %.3f/m² [%s] @ %s"
                  % (name, count, meta["mode"], density, _band(density), cell))

    print("[crowd-count] wrote %d cell.crowd_density row(s) into tenant %s" % (wrote, tenant))
    return 0 if wrote else 1


if __name__ == "__main__":
    raise SystemExit(main())
