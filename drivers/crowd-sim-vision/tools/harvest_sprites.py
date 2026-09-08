#!/usr/bin/env python3
"""Harvest real person sprites from the live camera feeds — a one-time DEV tool, not a governed component.

Runs inside the vision runtime image on the stack network. It reads recent media.frame for the real
(non-sim) cameras, fetches the bytes from the filestore, runs YOLO, and crops each detected person into
components/drivers/crowd-sim-vision/assets/sprites/. crowd-sim-vision then composites those real crops into
its simulated crowd frames, so the frames contain genuinely YOLO-detectable people (validated ~60% recall at
moderate density; detection saturates in dense crowds — see the density-estimation note in crowd-count).

WHY NOT COMMITTED AS DATA: the crops are photos of real, identifiable people from a broadcast feed. They are
a LOCALLY-regenerated runtime asset (like a model weight), never redistributed in git — assets/sprites/ is
gitignored. Re-run this to refill them for a given deployment.

Env: IDENTITY_URL, KERNEL_URL, FILESTORE_URL, ADMIN_USER (default nashik-admin), YOLO_WEIGHTS, OUT (/out),
MAX_SPRITES (18), MIN_CONF (0.55). Example:
  docker run --rm --network airawatos_default \
    -v $PWD/components/drivers/crowd-count/yolov8n.pt:/w/yolov8n.pt:ro -v /tmp/sprites:/out \
    -e YOLO_WEIGHTS=/w/yolov8n.pt manishsv/airawatos-runtime-vision:0.1.0 python3 harvest_sprites.py
"""
import json, os, urllib.request as u
import numpy as np, cv2
from ultralytics import YOLO

IDN = os.environ.get("IDENTITY_URL", "http://identity:8093")
K = os.environ.get("KERNEL_URL", "http://kernel:8080")
FS = os.environ.get("FILESTORE_URL", "http://filestore:8095")
ADMIN = os.environ.get("ADMIN_USER", "nashik-admin")
OUT = os.environ.get("OUT", "/out")
MAX_SPRITES = int(os.environ.get("MAX_SPRITES", "18"))
MIN_CONF = float(os.environ.get("MIN_CONF", "0.55"))
WEIGHTS = os.environ.get("YOLO_WEIGHTS", "/w/yolov8n.pt")


def _tok():
    return json.loads(u.urlopen(u.Request(IDN + "/login", data=json.dumps({"username": ADMIN}).encode(),
                      headers={"Content-Type": "application/json"}), timeout=15).read())["access_token"]


def _resolve(schema, tok):
    r = u.Request(K + "/v1/registry/resolve", data=json.dumps({"filter": {"schema": schema}, "limit": 9000}).encode(),
                  headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok}, method="POST")
    return [x.get("object", x) for x in json.loads(u.urlopen(r, timeout=30).read()).get("results", [])]


def main():
    os.makedirs(OUT, exist_ok=True)
    tok = _tok()
    m = YOLO(WEIGHTS)
    frames = [o for o in _resolve("media.frame", tok)
              if "fixture" in str(o.get("camera_ref", "")) or "sim" not in str(o.get("camera_ref", ""))]
    frames.sort(key=lambda o: o.get("captured_at", ""), reverse=True)
    saved = 0
    for fr in frames[:60]:
        if saved >= MAX_SPRITES:
            break
        try:
            b = u.urlopen(FS + "/files/" + u.quote(fr.get("file_id", ""), safe=":"), timeout=20).read()
        except Exception:
            continue
        arr = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            continue
        for r in m.predict(arr, classes=[0], verbose=False):
            for bx, cf in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy()):
                if saved >= MAX_SPRITES or float(cf) < MIN_CONF:
                    continue
                x1, y1, x2, y2 = [int(v) for v in bx]
                pad = int(0.08 * (y2 - y1))
                x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
                x2, y2 = min(arr.shape[1], x2 + pad), min(arr.shape[0], y2 + pad)
                if (x2 - x1) < 24 or (y2 - y1) < 48:
                    continue
                cv2.imwrite(os.path.join(OUT, "person%02d.png" % saved), arr[y1:y2, x1:x2])
                saved += 1
    print("harvested %d person crops to %s" % (saved, OUT))
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
