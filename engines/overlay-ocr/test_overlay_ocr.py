"""overlay-ocr: parsing (plain text) and one full read of a synthetic overlay (needs the ocr runtime)."""
import io, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run as oc


def test_timestamp_as_printed_with_ocr_mixups_fixed():
    assert oc.find_timestamp("10-16-2O26 Fri 16:O4:22") == "10-16-2026 Fri 16:04:22"
    assert oc.find_timestamp("x 16-10-2026  fri 16:05:10 y") == "16-10-2026 Fri 16:05:10"   # day-first kept as printed
    assert oc.find_timestamp("01-01-1970 Thu 00:58:54") == "01-01-1970 Thu 00:58:54"        # a reset clock is still read
    assert oc.find_timestamp("10/16/2026 Fri 16.04.22") == "10/16/2026".replace("/", "-") + " Fri 16:04:22"
    assert oc.find_timestamp("no stamp here") == ""


def test_name_prefers_a_from_line_and_ignores_noise():
    assert oc.find_name([("10-16-2026 Fri 16:04:22", 92), ("From Rave 3", 88)]) == "From Rave 3"
    assert oc.find_name([("FROM DEEP TIRAHA", 70)]) == "FROM DEEP TIRAHA"
    assert oc.find_name([("2036_171", 91)]) == "2036_171"                 # a short confident code
    assert oc.find_name([("~ri;", 40), ("ee", 60)]) == ""                 # low-confidence noise → nothing
    assert oc.find_name([]) == ""
    assert oc.find_name([("10-16-2026 Fri 16", 95), ("10", 90)]) == ""      # pieces of the stamp are not names
    assert oc.find_name([("From", 91)]) == ""                              # a bare "From" is no name


def test_sparse_words_are_rebuilt_into_lines():
    d = {"text": ["10-1", "O26", "Fri", "16:07:42", "From", "Ghat"], "conf": [91, 82, 85, 88, 90, 92],
         "left": [10, 80, 150, 200, 10, 90], "top": [5, 6, 5, 4, 60, 61], "height": [20, 20, 20, 20, 20, 20],
         "block_num": [1, 2, 3, 4, 5, 6], "par_num": [1] * 6, "line_num": [1] * 6}
    lines = oc._lines_from(d, True)
    assert [t for t, _c in lines] == ["10-1 O26 Fri 16:07:42", "From Ghat"], lines


def test_reads_a_synthetic_overlay():
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    a = np.random.default_rng(3).integers(40, 120, (720, 1280, 3)).astype("uint8")          # a dark, busy scene
    im = Image.fromarray(a); d = ImageDraw.Draw(im); f = ImageFont.load_default(size=34)
    d.text((24, 18), "10-16-2026 Fri 16:04:22", font=f, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    d.text((24, 660), "From Rave 3", font=f, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    b = io.BytesIO(); im.save(b, "JPEG", quality=90)
    ans = oc.answer(oc.read_lines(b.getvalue()))
    assert ans["timestamp"] == "10-16-2026 Fri 16:04:22", ans
    assert ans["camera_name"] == "From Rave 3", ans
