# PADDLEOCR runtime image (ADR-0030 pattern; ADR-0134). Reads text in images on the platform's own CPU — camera overlays,
# scanned registers, handwriting — so private frames and documents never leave for OCR. PaddlePaddle 3.0 (CPU) and
# PaddleOCR 3.0 (Apache-2.0) with the PP-OCRv5 server and mobile models and the page and text-line orientation
# models, all downloaded at BUILD time (components run with no network) into /opt/paddlex. Components that declare
# `runtime: { profile: paddleocr }` run here; their code is MOUNTED at /drivers by the launcher.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /runtime/requirements.txt
RUN pip install --no-cache-dir -r /runtime/requirements.txt

ENV PADDLE_PDX_CACHE_HOME=/opt/paddlex PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PYTHONUNBUFFERED=1 OMP_NUM_THREADS=4 FLAGS_use_mkldnn=1
COPY prefetch.py /runtime/prefetch.py
RUN python /runtime/prefetch.py && chmod -R a+rX /opt/paddlex

WORKDIR /drivers
ENV PYTHONPATH=/drivers
