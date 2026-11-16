"""image-quality-cv: measures and judgements on synthetic frames (no camera images)."""
import io, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run as iq


def _jpeg(arr):
    from PIL import Image
    b = io.BytesIO(); Image.fromarray(arr.astype("uint8")).save(b, "JPEG", quality=92); return b.getvalue()


def _texture(seed=1, h=540, w=960, base=140):
    import numpy as np
    rng = np.random.default_rng(seed)
    a = np.full((h, w, 3), base, dtype=np.float32)
    for _ in range(3000):                                  # dense small coloured rectangles: crisp edges, full contrast
        y, x = rng.integers(0, h - 20), rng.integers(0, w - 30)
        a[y:y + rng.integers(4, 20), x:x + rng.integers(4, 30)] = rng.integers(30, 230, 3)
    return a


def test_sharp_texture_reads_sharp_and_good_in_daylight():
    m = iq.measure(_jpeg(_texture()))
    j = iq.judge(m)
    assert j["A03"]["label"] == "sharp", m["lap_var"]
    assert j["A02"]["labels"][0] == "day" and j["A01"]["label"] == "good", (m, j)


def test_the_same_texture_blurred_reads_blurred_and_not_good():
    from scipy import ndimage
    a = _texture()
    for c in range(3):
        a[..., c] = ndimage.gaussian_filter(a[..., c], 6)
    j = iq.judge(iq.measure(_jpeg(a)))
    assert j["A03"]["label"] == "blurred" and j["A01"]["label"] != "good", j


def test_a_dark_frame_is_night_and_low_light():
    a = _texture() * 0.25
    j = iq.judge(iq.measure(_jpeg(a)))
    assert "night" in j["A02"]["labels"] and "low_light" in j["A02"]["labels"], j


def test_a_blank_frame_is_poor():
    import numpy as np
    j = iq.judge(iq.measure(_jpeg(np.full((540, 960, 3), 128))))
    assert j["A01"]["label"] == "poor", j


def test_a_large_saturated_patch_is_glare():
    a = _texture(); a[200:300, 300:450] = 255
    j = iq.judge(iq.measure(_jpeg(a)))
    assert "glare" in j["A02"]["labels"], j


def test_an_undecodable_frame_is_poor_and_never_claims_rain():
    assert iq.judge(None)["A01"]["label"] == "poor"
    try:
        iq.measure(b"not an image"); assert False, "should not decode"
    except Exception:
        pass
    j = iq.judge(iq.measure(_jpeg(_texture())))
    assert "rain" not in j["A02"]["labels"]


def test_overlay_bands_are_left_out():
    a = _texture() * 0.25; a[:40] = 255                    # a white timestamp strip across the top of a dark frame
    m = iq.measure(_jpeg(a))
    assert m["glare_blob"] < iq.GLARE_BLOB, m              # the strip is not glare
