"""Build-time: download every model this runtime provides into PADDLE_PDX_CACHE_HOME and run each once, so a component
never needs the network. Fails the build if a model does not load or read a test line."""
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from paddleocr import PaddleOCR

im = Image.new("RGB", (640, 120), "white"); d = ImageDraw.Draw(im)
d.text((20, 30), "10-16-2026 Fri 16:04:22", fill="black", font=ImageFont.load_default(size=40))
arr = np.asarray(im)
for kw in ({"text_detection_model_name": "PP-OCRv5_server_det", "text_recognition_model_name": "PP-OCRv5_server_rec"},
           {"text_detection_model_name": "PP-OCRv5_mobile_det", "text_recognition_model_name": "PP-OCRv5_mobile_rec"}):
    ocr = PaddleOCR(use_doc_orientation_classify=True, use_doc_unwarping=False, use_textline_orientation=True, **kw)
    res = ocr.predict(arr)
    texts = [t for r in res for t in r["rec_texts"]]
    print(kw, "→", texts)
    assert texts, "no text read with %s" % kw
print("prefetch ok")
