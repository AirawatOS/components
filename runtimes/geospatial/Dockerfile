# Geospatial runtime image (ADR-0030). Shared by every driver/engine that declares
# `runtime.profile: geospatial`; driver code is MOUNTED at /drivers by the launcher.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /runtime/requirements.txt
RUN pip install --no-cache-dir -r /runtime/requirements.txt

WORKDIR /drivers
ENV PYTHONPATH=/drivers PYTHONUNBUFFERED=1
