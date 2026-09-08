# VISION runtime image (ADR-0030). For components that declare `runtime.profile: vision` — object/person
# detection & counting (crowd-count). It exists for the SAME reason media/ does: ultralytics + opencv pull a
# ~250MB layer that only vision components want, so it does not belong in the shared reference image nor bloat
# every `modeling` engine. Built FROM the certified modeling image so torch/torchvision are REUSED, not
# re-downloaded — this layer only adds the vision-specific bits on top.
#
# Component code is MOUNTED read-only at /drivers by the launcher; the ONLY registry/file I/O is the broker
# (AIRAWAT_API). The pinned model weight ships in the component bundle (an `assets:` entry), not fetched at
# runtime — so a run is reproducible and offline.
FROM manishsv/airawatos-runtime-modeling:0.1.0

# opencv-python-headless (no libGL/X deps) for image decode; ultralytics for the YOLOv8 detector.
# libglib is the one shared lib opencv-headless still needs.
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "opencv-python-headless>=4.9" "ultralytics>=8.2" pillow

WORKDIR /drivers
ENV PYTHONPATH=/drivers PYTHONUNBUFFERED=1 YOLO_CONFIG_DIR=/tmp/ultralytics
