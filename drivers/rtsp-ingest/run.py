#!/usr/bin/env python3
"""rtsp-ingest driver: a live RTSP camera → media.frame (+ device.stream_health).

Confined — every registry read/write AND the content-addressed frame store go through the aos SDK's
broker-backed ctx (ctx.facts / ctx.get / ctx.write / ctx.file.put) under this run's token. ONE thing
dials out of the SDK: the RTSP socket itself, which ffmpeg opens directly because the broker speaks
HTTP and RTSP is a different protocol (see meta.yaml; ADR-0077 slice 2).

Reads the device.camera records this run is granted, grabs one frame per enabled camera, PUTs the JPEG
into the content-addressed file store, and writes:

  media.frame           citing file_id = sha2-256 of the bytes — so a detection that cites this frame
                        stays verifiable years later without trusting the transport
  device.stream_health  up | down, with the reason — a camera that silently stops is the most common
                        VMS failure, and it should be observable rather than buried in a log

It does NOT fabricate: a camera it cannot reach yields a `down` health observation and NO media.frame.
Exit is non-zero only if EVERY enabled camera failed, so one dead camera does not fail the whole run.

MIGRATION NOTE (aos SDK): the hand-rolled urllib/RUN_TOKEN/AIRAWAT_API broker plumbing and tenant
resolution are gone — ctx.facts / ctx.get / ctx.write / ctx.file own the registry + filestore transport,
retry and tenant stamping.
"""
import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone

from aos import driver, ctx

COMPONENT = "ds:airawatdeveloper:driver/rtsp-ingest"


def _ffmpeg_bin():
    """The host starts a component in a CLEAN environment (no ambient authority) — so PATH may not carry
    ffmpeg even where it is installed. Honour FFMPEG_BIN, else search PATH, else the usual absolute
    locations. Fail loudly rather than silently producing no frames."""
    explicit = os.environ.get("FFMPEG_BIN", "").strip()
    if explicit:
        return explicit
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if os.path.exists(cand):
            return cand
    return "ffmpeg"


def _slug(s):
    return re.sub(r"[^a-z0-9_-]", "-", str(s or "").lower()).strip("-") or "unknown"


def _cameras(camera_id):
    """The device.camera records this run may read. CAMERA_ID narrows it to one."""
    if camera_id:
        obj = ctx.get(camera_id, "device.camera")
        return [obj] if obj else []
    rows = list(ctx.facts("device.camera"))[:200]
    return [r.get("object", r) for r in rows]


def _last_capture_epoch(cam_id):
    """Epoch seconds of the most recent media.frame for this camera (0 if none). Lets the driver honour a
    per-camera stream.capture_interval_s: on a fast base schedule it captures a camera only once its own
    interval has elapsed. Reads media.frame (granted). NOTE: scans recent frames — fine at demo scale; a
    per-camera 'latest' index is the answer at hundreds of cameras."""
    rows = list(ctx.facts("media.frame"))[:500]
    best = 0.0
    for r in rows:
        o = r.get("object", r) or {}
        if o.get("camera_ref") != cam_id:
            continue
        try:
            e = datetime.fromisoformat(str(o.get("captured_at")).replace("Z", "+00:00")).timestamp()
        except Exception:
            e = 0.0
        if e > best:
            best = e
    return best


def _grab(url, transport, ffmpeg, quality, timeout_s):
    """One JPEG from the live stream, on stdout. Returns (bytes, error). ffmpeg opens this socket
    directly — the broker cannot proxy RTSP (meta.yaml)."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-rtsp_transport", transport or "tcp", "-i", url,
           "-frames:v", "1", "-q:v", quality, "-f", "image2", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None, "timeout after %ds — no frame arrived" % timeout_s
    except FileNotFoundError:
        return None, "ffmpeg not found (set FFMPEG_BIN)"
    if p.returncode != 0 or not p.stdout:
        return None, (p.stderr.decode("utf-8", "replace").strip().splitlines() or ["ffmpeg exited %d" % p.returncode])[-1][:200]
    return p.stdout, None


def _health(cam, state, detail, at):
    """One device.stream_health observation. Written whether the grab worked or not — that is the point."""
    tenant = ctx.tenant
    hid = "ds:%s:device/stream_health/%s/%s" % (tenant, _slug(cam.get("name") or cam.get("id")), at)
    obj = {"schema": "device.stream_health", "id": hid, "camera_ref": cam["id"], "state": state,
           "observed_at": at, "prov": {"component": COMPONENT, "at": at, "method": "ffmpeg-single-frame"}}
    if detail:
        obj["detail"] = detail
    ctx.write(obj)


@driver.main
def run():
    tenant = ctx.tenant
    if not tenant:
        print("[rtsp-ingest] no tenant in broker context — refusing to guess"); return 2

    camera_id = os.environ.get("CAMERA_ID", "").strip()          # limit the run to one camera
    ffmpeg = _ffmpeg_bin()
    quality = os.environ.get("FRAME_QUALITY", "3")               # ffmpeg -q:v, 2=best .. 31=worst
    timeout_s = int(os.environ.get("FFMPEG_TIMEOUT_S", "20"))
    default_interval_s = int(os.environ.get("DEFAULT_CAPTURE_INTERVAL_S", "300"))

    cams = [c for c in _cameras(camera_id) if c and c.get("enabled", True)]
    if not cams:
        print("[rtsp-ingest] no enabled device.camera readable by this run — nothing to do"); return 0

    ok = 0
    for cam in cams:
        at = ctx.now()
        stream = cam.get("stream") or {}
        url = stream.get("url")
        name = cam.get("name") or cam.get("id")
        # protocol split: this driver grabs live network feeds only. A 'sim' camera is driven by
        # crowd-sim-vision (which also reports its health) — touching it here only produces a spurious
        # 'down' ("Protocol not found"), so skip it entirely rather than fight over the same device.
        proto = (stream.get("protocol") or "").lower()
        if proto and proto not in ("rtsp", "http", "https", "hls", "rtmp"):
            print("[rtsp-ingest] %s: protocol '%s' is not this driver's job — skip" % (name, proto))
            continue
        if not url:
            print("[rtsp-ingest] %s: no stream.url" % name)
            _health(cam, "unknown", "no stream.url on the device.camera record", at)
            continue

        # per-camera capture cadence: on a fast base tick, only capture when this camera's interval elapsed
        interval = default_interval_s
        try:
            if stream.get("capture_interval_s") is not None:
                interval = int(float(stream.get("capture_interval_s")))
        except Exception:
            interval = default_interval_s
        if interval > 0:
            last = _last_capture_epoch(cam["id"])
            if last and (datetime.now(timezone.utc).timestamp() - last) < interval - 5:
                print("[rtsp-ingest] %s: not due yet (interval %ss) — skip" % (name, interval))
                continue

        blob, err = _grab(url, stream.get("transport"), ffmpeg, quality, timeout_s)
        if err:
            print("[rtsp-ingest] %s: DOWN — %s" % (name, err))
            _health(cam, "down", err, at)
            continue

        # Store the bytes first: the file id IS their sha2-256, and media.frame must cite a file that
        # already exists — never a record pointing at a blob that was never written.
        file_id = ctx.file.put(blob)               # content-addressed store (SDK filestore seam) → the id IS the sha2-256
        if not file_id:
            print("[rtsp-ingest] %s: file store returned no id" % name)
            _health(cam, "degraded", "frame captured but the file store returned no id", at)
            continue

        fid = "ds:%s:media/frame/%s/%s" % (tenant, _slug(name), at)
        frame = {"schema": "media.frame", "id": fid, "camera_ref": cam["id"], "captured_at": at,
                 "file_id": file_id, "media_type": "image/jpeg", "size_bytes": len(blob),
                 "prov": {"component": COMPONENT, "at": at, "method": "ffmpeg-single-frame",
                          "source": "rtsp"}}
        loc = cam.get("location") or {}
        if loc.get("h3"):
            frame["space"] = {"h3": loc["h3"]}
        ctx.write(frame)
        _health(cam, "up", None, at)
        ok += 1
        print("[rtsp-ingest] %s: UP — %d bytes → %s" % (name, len(blob), file_id))

    print("[rtsp-ingest] %d/%d camera(s) produced a frame into tenant %s" % (ok, len(cams), tenant))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(driver.run())
