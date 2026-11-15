#!/bin/sh
# Fetch the vision runtime's pinned inputs into ./build and check them — run on the build host before `docker build`.
# After building, move ./build OUT of the runtime folder (e.g. to ~/airawatos/rt-build/vision): the launcher checks
# the on-disk runtime folder against its certified digest, which covers only the files in the repo.
set -eu
cd "$(dirname "$0")"; mkdir -p build
RTDETR_SHA=df2a96c8c3a098567a1149bf0248da6efe6d495d
WEIGHTS_URL=https://huggingface.co/iisc-aim/UVH-26/resolve/main/weights/RT-DETRv2-S/UVH-26-MV-RT-DETRv2-S.pth
WEIGHTS_SHA256=$(sed -n 's/^weights_sha256: *\([0-9A-Za-z]*\).*/\1/p' meta.yaml)
if [ ! -d build/rtdetrv2_pytorch ]; then
  curl -fsSL "https://codeload.github.com/lyuwenyu/RT-DETR/tar.gz/$RTDETR_SHA" | tar xz -C build
  mv "build/RT-DETR-$RTDETR_SHA/rtdetrv2_pytorch" build/rtdetrv2_pytorch && rm -rf "build/RT-DETR-$RTDETR_SHA"
fi
[ -f build/UVH-26-MV-RT-DETRv2-S.pth ] || curl -fsSL -o build/UVH-26-MV-RT-DETRv2-S.pth "$WEIGHTS_URL"
GOT=$(sha256sum build/UVH-26-MV-RT-DETRv2-S.pth | cut -d' ' -f1)
if [ "$WEIGHTS_SHA256" = "PENDING" ]; then echo "weights sha256 (record in meta.yaml): $GOT"; elif [ "$GOT" != "$WEIGHTS_SHA256" ]; then echo "weights sha256 mismatch: $GOT" >&2; exit 1; fi
echo "fetched: rtdetrv2_pytorch@$RTDETR_SHA, weights $GOT"
