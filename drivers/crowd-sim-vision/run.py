#!/usr/bin/env python3
"""crowd-sim-vision — the IMAGE front-end for the crush-risk demo (vision-crowd-counting slice).

The number-injection simulator (kumbh-crowd-sim) writes cell.crowd_density directly. This writes the layer
BEFORE that — a crowd IMAGE per camera per tick — so the loop exercises real PERCEPTION:

    crowd-sim-vision → media.frame → crowd-count (YOLO) → cell.crowd_density → crush-risk-engine

Confined: the only I/O is the broker (X-Run-Token). Per enabled device.camera it produces one JPEG with a
CONTROLLABLE headcount K, PUTs the bytes into the content-addressed store, and writes media.frame stamping the
ground truth in prov.sim_headcount — so (a) the detector can be validated against truth and (b) the whole loop
runs before the vision model is provisioned (crowd-count falls back to this ground truth).

K is driven by a scenario curve (baseline | ramkund-crush) shaped like the real Nashik peak, scaled per camera
by an optional `crowd_level` in the camera record. Two image modes:
  composite (default) — paste K person sprites (assets/sprites/*.png) onto a background (assets/bg.jpg). This is
                        the SIMULATION: exact ground truth AND real detectable people. No sprites present →
                        draws K dark silhouettes (K still exact; not YOLO-detectable — the count engine then
                        uses the stamped ground truth, and it is logged loudly).
  bank               — cycle REAL crowd photos dropped in assets/bank/, count read from the filename `_<n>`
                        (honest real imagery; no fabrication — see no-synthetic-data).
"""
import base64, io, json, math, os, random, urllib.request
from datetime import datetime, timezone

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
COMPONENT = "ds:airawatdeveloper:driver/crowd-sim-vision"
HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.environ.get("SOURCE", "composite")               # composite | bank
SCENARIO = os.environ.get("SCENARIO", "ramkund-crush")       # baseline | ramkund-crush
BASELINE_K = int(os.environ.get("BASELINE_K", "40"))         # per-camera crowd at the scenario trough
PEAK_K = int(os.environ.get("PEAK_K", "900"))                # per-camera crowd at the scenario peak
W, H = int(os.environ.get("IMG_W", "1280")), int(os.environ.get("IMG_H", "720"))
CAMERA_ID = os.environ.get("CAMERA_ID", "").strip()
SEED_HOUR = os.environ.get("SIM_HOUR")                        # pin the scenario hour for a repeatable demo


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


def _health(tenant, cam, state, detail, at):
    """Report the sim camera's delivery state so the operator console shows it live (not a spurious 'down').
    crowd-sim-vision owns these cameras' health — it is the thing actually producing their frames."""
    cid_tail = str(cam.get("id", "cam")).rsplit("/", 1)[-1]
    hid = "ds:%s:device/stream_health/%s/%s" % (tenant, cid_tail, at)
    obj = {"schema": "device.stream_health", "id": hid, "camera_ref": cam["id"], "state": state,
           "observed_at": at, "prov": {"component": COMPONENT, "at": at, "method": "crowd-image-sim"}}
    if detail:
        obj["detail"] = detail
    try:
        _call("/v1/write", {"scope": "device.stream_health", "id": hid, "object": obj})
    except Exception as e:
        print("[crowd-sim-vision] health write failed for %s: %s" % (cam.get("name"), str(e)[:80]))


def _cameras():
    if CAMERA_ID:
        obj = (_call("/v1/read", {"scope": "device.camera", "id": CAMERA_ID}) or {}).get("object")
        return [obj] if obj else []
    rows = (_call("/v1/query", {"scope": "device.camera", "limit": 500}) or {}).get("results", [])
    return [r.get("object", r) for r in rows]


def _scenario_k(cam):
    """The controllable ground-truth headcount for this camera this tick — a peak curve like the real Nashik
    13:00–14:00 pinch, scaled by the camera's optional crowd_level (0..1, default 1)."""
    hour = int(SEED_HOUR) if SEED_HOUR is not None else datetime.now(timezone.utc).hour
    sigma = 2.6 if SCENARIO == "baseline" else 2.2
    curve = math.exp(-((hour - 13) ** 2) / (2 * sigma ** 2))          # peaks at 13:00
    k = BASELINE_K + (PEAK_K - BASELINE_K) * curve
    if SCENARIO == "ramkund-crush" and hour in (13, 14):
        k *= 1.35                                                     # the sharp surge
    level = (cam.get("location") or {}).get("crowd_level", cam.get("crowd_level"))
    if level is not None:
        try: k *= float(level)
        except Exception: pass
    return max(0, int(round(k)))


# ---- image generation --------------------------------------------------------------------------------------
def _composite(k):
    """A background with K person sprites pasted at random ground positions → (jpeg_bytes, detectable)."""
    from PIL import Image, ImageDraw
    bg_path = os.path.join(HERE, "assets", "bg.jpg")
    if os.path.exists(bg_path):
        img = Image.open(bg_path).convert("RGB").resize((W, H))
    else:
        img = Image.new("RGB", (W, H), (120, 120, 120))              # neutral plaza ground
    sprite_dir = os.path.join(HERE, "assets", "sprites")
    sprites = ([os.path.join(sprite_dir, f) for f in sorted(os.listdir(sprite_dir))
                if f.lower().endswith((".png",))] if os.path.isdir(sprite_dir) else [])
    rnd = random.Random(k * 7919 + (int(SEED_HOUR) if SEED_HOUR is not None else 0))
    detectable = bool(sprites)
    draw = ImageDraw.Draw(img)
    for _ in range(k):
        y = rnd.randint(int(H * 0.35), H - 10)                       # perspective: nearer = lower + bigger
        scale = 0.25 + 0.75 * (y - H * 0.35) / (H * 0.65)
        ph = max(12, int(90 * scale)); pw = max(5, int(ph * 0.4))
        x = rnd.randint(0, W - pw)
        if detectable:
            sp = Image.open(rnd.choice(sprites)).convert("RGBA").resize((pw, ph))
            img.paste(sp, (x, y - ph), sp)
        else:
            draw.ellipse([x, y - ph, x + pw, y], fill=(35, 35, 40))  # silhouette placeholder (not detectable)
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=80)
    return buf.getvalue(), detectable


def _from_bank(k_target):
    """Pick the REAL crowd photo whose filename count (`_<n>` before the extension) is nearest k_target.
    Returns (jpeg_bytes, true_count). No bank → None (fail loudly, never fabricate)."""
    bank = os.path.join(HERE, "assets", "bank")
    if not os.path.isdir(bank):
        return None, None
    best = None
    for f in os.listdir(bank):
        if not f.lower().endswith((".jpg", ".jpeg")):
            continue
        try:
            n = int(f.rsplit("_", 1)[-1].split(".")[0])
        except Exception:
            continue
        if best is None or abs(n - k_target) < abs(best[1] - k_target):
            best = (f, n)
    if not best:
        return None, None
    with open(os.path.join(bank, best[0]), "rb") as fh:
        return fh.read(), best[1]


def main():
    tenant = _context().get("tenant")
    if not tenant:
        print("[crowd-sim-vision] no tenant in broker context — refusing to guess"); return 2
    # ONLY sim cameras — never overwrite a real feed's frames. rtsp-ingest owns the network-protocol cameras.
    cams = [c for c in _cameras() if c and c.get("enabled", True)
            and ((c.get("stream") or {}).get("protocol") or "").lower() == "sim"]
    if not cams:
        print("[crowd-sim-vision] no enabled sim device.camera — nothing to do"); return 0

    wrote = 0
    for cam in cams:
        name = cam.get("name") or cam.get("id")
        k = _scenario_k(cam)
        detectable = True
        if SOURCE == "bank":
            blob, true_k = _from_bank(k)
            if blob is None:
                print("[crowd-sim-vision] %s: SOURCE=bank but assets/bank empty — skip (no fabrication)" % name)
                continue
            k = true_k
        else:
            try:
                blob, detectable = _composite(k)
            except Exception as e:
                print("[crowd-sim-vision] %s: composite failed (%s) — needs pillow (vision runtime)" % (name, str(e)[:100]))
                continue

        put = _call("/v1/file/put", {"content_b64": base64.b64encode(blob).decode()})
        file_id = (put or {}).get("id")
        if not file_id:
            print("[crowd-sim-vision] %s: file store returned no id" % name); continue

        at = _now()
        fid = "ds:%s:media/frame/%s/%s" % (tenant, cam["id"].rsplit("/", 1)[-1], at)
        frame = {"schema": "media.frame", "id": fid, "camera_ref": cam["id"], "captured_at": at,
                 "file_id": file_id, "media_type": "image/jpeg", "size_bytes": len(blob),
                 "prov": {"component": COMPONENT, "at": at, "method": "crowd-image-sim", "source": "simulation",
                          "scenario": SCENARIO, "sim_headcount": k, "detectable": detectable,
                          "note": "SIMULATED crowd image — not a real camera feed"}}
        loc = cam.get("location") or {}
        if loc.get("h3"):
            frame["space"] = {"h3": loc["h3"]}
        _call("/v1/write", {"scope": "media.frame", "id": fid, "object": frame})
        _health(tenant, cam, "up", "simulated feed (crowd-sim-vision)", at)
        wrote += 1
        print("[crowd-sim-vision] %s: K=%d %s → %d bytes → %s"
              % (name, k, "(detectable)" if detectable else "(silhouettes; ground-truth only)", len(blob), file_id))

    print("[crowd-sim-vision] wrote %d simulated media.frame(s) into tenant %s [%s]" % (wrote, tenant, SCENARIO))
    return 0 if wrote else 1


if __name__ == "__main__":
    raise SystemExit(main())
