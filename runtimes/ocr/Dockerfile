# OCR runtime image (ADR-0030 pattern; ADR-0134). Reads text in images on the platform's own CPU, so private camera
# frames and documents never leave for OCR: Tesseract 5 (Apache-2.0) with its English model, Pillow and numpy for
# preparing the text regions. Shared by every component that declares `runtime: { profile: ocr }`; confined like any
# other (no network at run time). Component code is MOUNTED at /drivers by the launcher.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /runtime/requirements.txt
RUN pip install --no-cache-dir -r /runtime/requirements.txt

WORKDIR /drivers
ENV PYTHONPATH=/drivers PYTHONUNBUFFERED=1 OMP_THREAD_LIMIT=2
