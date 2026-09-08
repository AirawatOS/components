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

# The shared libs OpenCV's cv2 dlopens at import. ultralytics hard-depends on opencv-python (the FULL, non-
# headless build), so even though we force the headless wheel below, we install the small X/GL runtime libs so
# import never fails on a slim base (the libxcb.so.1 / libGL.so.1 class of error).
RUN apt-get update && apt-get install -y --no-install-recommends \
      libglib2.0-0 libgl1 libxcb1 libsm6 libxext6 libxrender1 \
 && rm -rf /var/lib/apt/lists/*
# Install ultralytics first (it drags in opencv-python), THEN force opencv-python-headless to own cv2 — the two
# distributions ship the same module, so last-installed wins. pillow for the sim compositor.
RUN pip install --no-cache-dir "ultralytics>=8.2" pillow \
 && pip uninstall -y opencv-python opencv-python-headless >/dev/null 2>&1 || true \
 && pip install --no-cache-dir "opencv-python-headless>=4.9"

WORKDIR /drivers
ENV PYTHONPATH=/drivers PYTHONUNBUFFERED=1 YOLO_CONFIG_DIR=/tmp/ultralytics
