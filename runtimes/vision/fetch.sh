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
check() {  # file, expected sha256 (PENDING = print it, to be recorded in meta.yaml)
  got=$(sha256sum "$1" | cut -d' ' -f1)
  if [ "$2" = "PENDING" ]; then echo "$1 sha256 (record in meta.yaml): $got"; elif [ "$got" != "$2" ]; then echo "$1 sha256 mismatch: $got" >&2; exit 1; fi
}
[ -f build/UVH-26-MV-RT-DETRv2-S.pth ] || curl -fsSL -o build/UVH-26-MV-RT-DETRv2-S.pth "$WEIGHTS_URL"
check build/UVH-26-MV-RT-DETRv2-S.pth "$WEIGHTS_SHA256"
COCO_URL=https://github.com/lyuwenyu/storage/releases/download/v0.2/rtdetrv2_r18vd_120e_coco_rerun_48.1.pth
COCO_SHA256=$(sed -n 's/^coco_weights_sha256: *\([0-9A-Za-z]*\).*/\1/p' meta.yaml)
[ -f build/rtdetrv2_r18vd_120e_coco_rerun_48.1.pth ] || curl -fsSL -o build/rtdetrv2_r18vd_120e_coco_rerun_48.1.pth "$COCO_URL"
check build/rtdetrv2_r18vd_120e_coco_rerun_48.1.pth "$COCO_SHA256"
L_URL=https://github.com/lyuwenyu/storage/releases/download/v0.1/rtdetrv2_r50vd_2x_coco_objects365_from_paddle.pth
L_SHA256=$(sed -n 's/^o365_l_weights_sha256: *\([0-9A-Za-z]*\).*/\1/p' meta.yaml)
[ -f build/rtdetrv2_r50vd_2x_coco_objects365_from_paddle.pth ] || curl -fsSL -o build/rtdetrv2_r50vd_2x_coco_objects365_from_paddle.pth "$L_URL"
check build/rtdetrv2_r50vd_2x_coco_objects365_from_paddle.pth "$L_SHA256"
echo "fetched: rtdetrv2_pytorch@$RTDETR_SHA, UVH-26, COCO-S and Objects365-L weights checked"
