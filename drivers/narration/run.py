"""narration — text → speech audio, generated once and kept (Sarvam Bulbul through the broker).

The registry is the cache:
  media.narration_clip     id ds:<tenant>:media/narration_clip/<key>     text + voice settings → file_id (filestore, sha2-256)
  media.narration_request  id ds:<tenant>:media/narration_request/<key>  a player's ask for a line with no clip (status pending →
                                                              done | failed)
key = narration_key(language, voice, model, pace, text) — sha-256, first 24 hex chars, of the NORMALISED text and the
voice settings; a player computes the same key to look a line up, so a voice change is a new clip, never an overwrite.

Each run: the pending requests (+ NARRATE_TEXT, if set) → skip any that already has a clip → synthesise the rest via
the broker's /v1/fetch to api.sarvam.ai with SARVAM_API_KEY injected from Vault as the api-subscription-key header (the
driver never sees the key) → /v1/file/put → write media.narration_clip → mark the request done. A failure marks the request
failed with the reason; it is retried on the next run up to 3 attempts. Idempotent; never plays anything.

env: NARRATION_LANGUAGE (en-IN) · NARRATION_VOICE (shubh) · NARRATION_MODEL (bulbul:v3) · NARRATION_PACE (1.0) — the
defaults for NARRATE_TEXT and for a request that names none · NARRATE_TEXT (one line, optional) · MAX_LINES (200/run)
"""
import base64, hashlib, json, os, re, sys, time, urllib.error, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ.get("RUN_TOKEN", "")
TENANT = os.environ.get("TENANT") or os.environ.get("WRITE_TENANT") or ""
SARVAM_URL = "https://api.sarvam.ai/text-to-speech"
SRC = "ds:airawat:ref/source/sarvam"
DEFAULTS = {"language": os.environ.get("NARRATION_LANGUAGE") or "en-IN",
            "voice": os.environ.get("NARRATION_VOICE") or "shubh",
            "model": os.environ.get("NARRATION_MODEL") or "bulbul:v3",
            "pace": float(os.environ.get("NARRATION_PACE") or 1.0)}
MAX_LINES = int(os.environ.get("MAX_LINES") or 200)
MAX_ATTEMPTS = 3
MAX_CHARS = 2500                     # Sarvam REST limit for bulbul:v3


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def norm_text(t):
    return re.sub(r"\s+", " ", str(t or "")).strip()


def narration_key(language, voice, model, pace, text):
    """The lookup key a player computes too: sha-256 of 'language|voice|model|pace|text', first 24 hex chars."""
    s = "%s|%s|%s|%s|%s" % (language, voice, model, ("%.2f" % float(pace)), norm_text(text))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:24]


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "X-Run-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read() or b"{}")


def unwrap(r):
    return r.get("object") if isinstance(r, dict) and isinstance(r.get("object"), dict) else r


def query(scope, filt=None):
    out, after = [], None
    for _ in range(50):
        body = {"scope": scope, "limit": 1000}
        if filt:
            body["filter"] = filt
        if after:
            body["after"] = after
        res = call("/v1/query", body)
        rows = [unwrap(x) for x in (res.get("results") or [])]
        out += [o for o in rows if str(o.get("id", "")).startswith("ds:%s:" % TENANT)]
        after = res.get("next_cursor") or res.get("cursor")
        if not after or not rows:
            break
    return out


def write(scope, obj):
    t = now()
    call("/v1/write", {"scope": scope, "id": obj["id"], "object": obj, "valid_time": {"from": t}, "asserted_at": t})


def audio_kind(b):
    if b[:4] == b"RIFF":
        return "audio/wav"
    if b[:3] == b"ID3" or (len(b) > 1 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    return "application/octet-stream"


def synthesise(text, s):
    """Sarvam /text-to-speech through the broker; the key is injected from Vault. → audio bytes."""
    body = {"text": text, "language_code": s["language"], "speaker": s["voice"], "model": s["model"],
            "pace": float(s["pace"]), "speech_sample_rate": 24000, "output_audio_codec": "mp3"}
    last = ""
    for attempt in range(4):
        try:
            res = call("/v1/fetch", {"url": SARVAM_URL, "method": "POST", "body": body,
                                     "inject": {"name": "SARVAM_API_KEY", "as": "header", "header": "api-subscription-key", "prefix": ""}})
        except urllib.error.HTTPError as e:
            last = "broker %s: %s" % (e.code, (e.read() or b"")[:200].decode(errors="replace"))
            if e.code == 429 and attempt < 3:
                time.sleep(5 * (attempt + 1)); continue
            raise RuntimeError(last)
        st = res.get("upstream_status")
        if st == 200:
            payload = json.loads(res.get("body") or "{}")
            return base64.b64decode((payload.get("audios") or [""])[0])
        last = "sarvam %s: %s" % (st, str(res.get("body") or res.get("error") or "")[:200])
        if st in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(4 * (attempt + 1)); continue
        raise RuntimeError(last)
    raise RuntimeError(last or "sarvam: no response")


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — a driver runs in the context of exactly one tenant.")
    clips = {o.get("key"): o for o in query("media.narration_clip") if o.get("key")}
    todo = []
    for r in query("media.narration_request"):
        if r.get("status") == "done" or (r.get("status") == "failed" and int(r.get("attempts") or 0) >= MAX_ATTEMPTS):
            continue
        todo.append(r)
    one = norm_text(os.environ.get("NARRATE_TEXT"))
    if one:
        k = narration_key(DEFAULTS["language"], DEFAULTS["voice"], DEFAULTS["model"], DEFAULTS["pace"], one)
        todo.append({"id": "ds:%s:media/narration_request/%s" % (TENANT, k), "key": k, "text": one, **DEFAULTS, "status": "pending"})
    print("tenant=%s · %d clips · %d open requests · default voice %s/%s/%s" % (
        TENANT, len(clips), len(todo), DEFAULTS["language"], DEFAULTS["voice"], DEFAULTS["model"]), flush=True)

    made = reused = failed = 0
    for r in todo[:MAX_LINES]:
        s = {k: r.get(k) or DEFAULTS[k] for k in ("language", "voice", "model", "pace")}
        text = norm_text(r.get("text"))
        key = narration_key(s["language"], s["voice"], s["model"], s["pace"], text)
        req_id = r.get("id") or "ds:%s:media/narration_request/%s" % (TENANT, key)
        base = {"schema": "media.narration_request", "id": req_id, "tenant": TENANT, "key": key, "text": text,
                **s, "requested_at": r.get("requested_at") or now(), "requested_by": r.get("requested_by") or "driver/narration"}
        if not text or len(text) > MAX_CHARS or (r.get("key") and r.get("key") != key):
            why = "empty text" if not text else ("longer than %d characters" % MAX_CHARS if len(text) > MAX_CHARS
                                                 else "key does not match its text and voice settings")
            write("media.narration_request", dict(base, status="failed", error=why, attempts=MAX_ATTEMPTS, updated_at=now()))
            failed += 1; print("  ✗ %s: %s" % (key, why), flush=True); continue
        if key in clips:
            write("media.narration_request", dict(base, status="done", clip_ref=clips[key]["id"], updated_at=now()))
            reused += 1; continue
        try:
            audio = synthesise(text, s)
            if len(audio) < 200:
                raise RuntimeError("audio too short (%d bytes)" % len(audio))
            fid = call("/v1/file/put", {"content_b64": base64.b64encode(audio).decode()}).get("id")
            if not fid:
                raise RuntimeError("filestore returned no id")
            t = now()
            clip = {"schema": "media.narration_clip", "id": "ds:%s:media/narration_clip/%s" % (TENANT, key), "tenant": TENANT, "key": key,
                    "text": text, **s, "provider": "sarvam", "file_id": fid, "media_type": audio_kind(audio),
                    "size_bytes": len(audio), "created_at": t,
                    "prov": {"source_ref": SRC, "method": "sarvam-text-to-speech", "model": s["model"], "observed_at": t}}
            write("media.narration_clip", clip); clips[key] = clip
            write("media.narration_request", dict(base, status="done", clip_ref=clip["id"], updated_at=t))
            made += 1
            print("  ✓ %s %s (%d KB) %s" % (key, clip["media_type"], len(audio) // 1024, text[:70]), flush=True)
        except Exception as e:
            att = int(r.get("attempts") or 0) + 1
            write("media.narration_request", dict(base, status="failed", error=str(e)[:300], attempts=att, updated_at=now()))
            failed += 1
            print("  ✗ %s (attempt %d): %s" % (key, att, str(e)[:200]), flush=True)
            if "not set" in str(e) or "401" in str(e) or "403" in str(e):
                print("  stopping: the provider key is missing or rejected — set SARVAM_API_KEY in Component Settings", flush=True)
                break
    print("RESULT: " + json.dumps({"generated": made, "reused": reused, "failed": failed, "open_left": max(0, len(todo) - MAX_LINES)}))
    return 1 if failed and not (made or reused) else 0


if __name__ == "__main__":
    sys.exit(main())
