# Documents runtime image (ADR-0030 pattern). Shared by every driver/engine that declares
# `runtime.profile: documents` — text extraction from official documents (PDF today; OCR for scanned PDFs later).
# Driver code is MOUNTED at /drivers by the launcher.
FROM python:3.12-slim

COPY requirements.txt /runtime/requirements.txt
RUN pip install --no-cache-dir -r /runtime/requirements.txt

WORKDIR /drivers
ENV PYTHONPATH=/drivers PYTHONUNBUFFERED=1
