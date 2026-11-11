# Modeling runtime image (ADR-0030). Shared by every engine that declares `runtime.profile: modeling`;
# code is MOUNTED at /drivers by the launcher. torch is heavy — this keeps it out of the coordinator.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /runtime/requirements.txt
# generous download timeout + retries: the torch wheels are large and a dropped connection otherwise fails the build
ENV PIP_DEFAULT_TIMEOUT=300 PIP_RETRIES=10
RUN pip install --no-cache-dir -r /runtime/requirements.txt

WORKDIR /drivers
ENV PYTHONPATH=/drivers PYTHONUNBUFFERED=1
