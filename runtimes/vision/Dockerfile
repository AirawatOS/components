# VISION runtime image (ADR-0030 pattern; ADR-0134). CPU inference for image models evaluated in Model Lab and, later,
# used by the platform: object detectors first (RT-DETRv2 trained on UVH-26, Indian traffic). Components that declare
# `runtime: { profile: vision }` run here, confined like any other — no network, so the model code and weights are
# built INTO the image and covered by its certified digest. Component code is MOUNTED at /drivers by the launcher.
FROM python:3.11-slim

COPY requirements.txt /runtime/requirements.txt
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 torchvision==0.20.1 \
 && pip install --no-cache-dir -r /runtime/requirements.txt

# RT-DETRv2 reference code (lyuwenyu/RT-DETR, Apache-2.0) at a pinned commit, fetched and checked by fetch.sh.
COPY build/rtdetrv2_pytorch /opt/rtdetrv2
COPY uvh26_rtdetrv2_s.yml /opt/rtdetrv2/configs/rtdetrv2/uvh26_rtdetrv2_s.yml
# UVH-26 RT-DETRv2-S weights (AIM@IISc, Apache-2.0), checked against the sha256 in meta.yaml by fetch.sh.
COPY build/UVH-26-MV-RT-DETRv2-S.pth /opt/models/uvh26/rtdetrv2_s.pth

WORKDIR /drivers
ENV PYTHONPATH=/drivers:/opt/rtdetrv2 PYTHONUNBUFFERED=1 OMP_NUM_THREADS=4 TORCH_HOME=/tmp/torch
