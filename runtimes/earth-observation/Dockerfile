# Earth-Observation runtime image (ADR-0030). Shared by every driver that declares
# `runtime.profile: earth-observation` — the driver code is MOUNTED at /drivers by the launcher; this image
# only carries the common EO/GEE library set. Keeps earthengine-api/rasterio OUT of the host + coordinator.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /runtime/requirements.txt
RUN pip install --no-cache-dir -r /runtime/requirements.txt

# Driver code is mounted read-only at /drivers; its ONLY I/O is the broker (AIRAWAT_API) over the network.
# The launcher runs:  docker run --rm -v <driver-dir>:/drivers:ro -e RUN_TOKEN=… -e AIRAWAT_API=… \
#                       airawatos/runtime-earth-observation:0.1.0  python /drivers/run.py
WORKDIR /drivers
ENV PYTHONPATH=/drivers PYTHONUNBUFFERED=1
