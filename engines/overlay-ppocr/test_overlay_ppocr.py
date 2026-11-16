"""overlay-ppocr: the shared parsing rules, and one full PP-OCRv5 read of a synthetic overlay (needs the paddleocr runtime)."""
import io, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run as oc


def test_parsing_rules_match_overlay_ocr():
    assert oc.find_timestamp("10-16-2O26 Fri 16:O4:22") == "10-16-2026 Fri 16:04:22"
    assert oc.find_name([("10-16-2026 Fri 16:04:22", 92), ("From Rave 3", 88)]) == "From Rave 3"
    assert oc.find_name([("From", 99)]) == "" and oc.find_name([("10-16-2026 Fri 16", 95)]) == ""


def test_reads_a_synthetic_overlay():
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    a = np.zeros((720, 1280, 3), "uint8"); a[...] = np.linspace(50, 110, 1280)[None, :, None].astype("uint8")   # a smooth scene (noise makes thousands of false boxes)
    im = Image.fromarray(a); d = ImageDraw.Draw(im); f = ImageFont.load_default(size=30)
    d.text((24, 18), "10-16-2026 Fri 16:04:22", font=f, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    d.text((24, 665), "From Rave 3", font=f, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    b = io.BytesIO(); im.save(b, "JPEG", quality=85)
    ans = oc.answer(oc.read_lines(b.getvalue()))
    assert ans["timestamp"] == "10-16-2026 Fri 16:04:22", ans
    assert ans["camera_name"].replace(" ", "") == "FromRave3", ans          # PP-OCR may drop a space; the scorer's CER counts it
