#!/usr/bin/env python3
"""Generate recorded tour narration with Sarvam AI's Bulbul text-to-speech.

Input: the tour's spoken lines, as Visualize lists them — in the app, `await AOS_TOUR.script()` (a dry run of the
tour) returns [{key, text}]; save that JSON. Output: <out>/clips/<key>.<ext> + <out>/manifest.json, which the app
reads from `narration/` next to its index.html (key = FNV-1a 32 of the text; the app computes the same).

The API key is read from a private file (default ~/.sarvam-key, chmod 600) and never printed or written anywhere.

    python3 tools/narration/generate_sarvam.py lines.json apps/city-scan/dist/narration            # all lines
    python3 tools/narration/generate_sarvam.py lines.json /tmp/voices --sample shubh,ishita,priya  # try voices on 1 line

Re-running only synthesises lines without a clip (or all with --force, e.g. after changing the speaker); --prune
deletes clips no longer in the script. API: POST https://api.sarvam.ai/text-to-speech (header api-subscription-key).
"""
import argparse, base64, json, os, sys, time, urllib.error, urllib.request

API = "https://api.sarvam.ai/text-to-speech"


def fnv(text):
    h = 0x811c9dc5
    for b in text.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return "%08x" % h


def synth(key, text, a):
    body = {"text": text, "language_code": a.language, "speaker": a.speaker, "model": a.model,
            "pace": a.pace, "speech_sample_rate": a.rate, "output_audio_codec": a.codec}
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "api-subscription-key": key})
    for attempt in range(5):
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=120).read())
            return base64.b64decode(d["audios"][0])
        except urllib.error.HTTPError as e:
            msg = (e.read() or b"")[:300].decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 * (attempt + 1)); continue
            raise SystemExit(f"Sarvam HTTP {e.code}: {msg}")
        except urllib.error.URLError as e:
            if attempt < 4:
                time.sleep(2 * (attempt + 1)); continue
            raise SystemExit(f"Sarvam unreachable: {e.reason}")


def ext_of(audio):
    if audio[:4] == b"RIFF":
        return "wav"
    if audio[:3] == b"ID3" or (len(audio) > 1 and audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0):
        return "mp3"
    return "bin"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lines"); ap.add_argument("out")
    ap.add_argument("--key-file", default=os.path.expanduser("~/.sarvam-key"))
    ap.add_argument("--model", default="bulbul:v3")
    ap.add_argument("--speaker", default="shubh")
    ap.add_argument("--language", default="en-IN")
    ap.add_argument("--pace", type=float, default=1.0)
    ap.add_argument("--rate", type=int, default=24000)
    ap.add_argument("--codec", default="mp3")
    ap.add_argument("--sample", help="comma-separated speakers: render the first intro line once per speaker")
    ap.add_argument("--force", action="store_true"); ap.add_argument("--prune", action="store_true")
    a = ap.parse_args()

    try:
        key = open(a.key_file).read().strip()
    except OSError:
        raise SystemExit(f"no API key file at {a.key_file} (put the Sarvam key there, chmod 600)")
    if not key:
        raise SystemExit(f"{a.key_file} is empty")

    lines = json.load(open(a.lines))
    for ln in lines:                                   # the key must be what the app will look up
        if fnv(ln["text"]) != ln["key"]:
            raise SystemExit(f"key mismatch for {ln['text'][:60]!r}: {ln['key']} != {fnv(ln['text'])}")
    os.makedirs(os.path.join(a.out, "clips"), exist_ok=True)

    if a.sample:
        text = lines[0]["text"]
        for sp in [x.strip() for x in a.sample.split(",") if x.strip()]:
            a.speaker = sp
            audio = synth(key, text, a)
            path = os.path.join(a.out, f"sample-{sp}.{ext_of(audio)}")
            open(path, "wb").write(audio); print(f"{sp}: {path} ({len(audio)//1024} KB)")
        return

    mpath = os.path.join(a.out, "manifest.json")
    man = json.load(open(mpath)) if os.path.exists(mpath) else {}
    same_voice = all(man.get(k) == v for k, v in (("model", a.model), ("speaker", a.speaker), ("language", a.language), ("pace", a.pace)))
    clips = dict(man.get("clips") or {}) if same_voice and not a.force else {}
    made = kept = 0
    for i, ln in enumerate(lines, 1):
        k, text = ln["key"], ln["text"]
        if k in clips and os.path.exists(os.path.join(a.out, clips[k])):
            kept += 1; continue
        audio = synth(key, text, a)
        rel = f"clips/{k}.{ext_of(audio)}"
        open(os.path.join(a.out, rel), "wb").write(audio)
        clips[k] = rel; made += 1
        print(f"[{i}/{len(lines)}] {rel} ({len(audio)//1024} KB) {text[:70]}")
    want = {ln["key"] for ln in lines}
    clips = {k: v for k, v in clips.items() if k in want}
    if a.prune:
        keep = set(clips.values())
        for f in os.listdir(os.path.join(a.out, "clips")):
            if f"clips/{f}" not in keep:
                os.remove(os.path.join(a.out, "clips", f))
    json.dump({"provider": "sarvam", "model": a.model, "speaker": a.speaker, "language": a.language, "pace": a.pace,
               "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "clips": clips,
               "text": {ln["key"]: ln["text"] for ln in lines}}, open(mpath, "w"), indent=1, ensure_ascii=False)
    print(f"manifest: {len(clips)} clips ({made} new, {kept} kept) → {mpath}")


if __name__ == "__main__":
    sys.exit(main())
