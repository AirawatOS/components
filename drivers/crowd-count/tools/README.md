# crowd-count model tools

- `build_csrnet.py` — builds the density-estimation model asset `crowd-density.pt` (TorchScript).
  Downloads a pretrained CSRNet (ShanghaiTech Part B, MIT) from HuggingFace `rootstrap-org/crowd-counting`
  (`weights.pth`), loads it into the standard CSRNet arch (frontend VGG16-10 + dilated backend + 1×1 output;
  loads with 0 missing / 0 unexpected keys), and traces it to `crowd-density.pt`. Run in the vision runtime:

      docker run --rm -v $PWD:/w -v $PWD/out:/out manishsv/airawatos-runtime-vision:0.1.0 python3 /w/build_csrnet.py

  The resulting `crowd-density.pt` is a LOCAL runtime asset (gitignored) — regenerate per deployment.

Counting methods (COUNT_METHOD): `detection` (YOLO, default) | `density` (this CSRNet). Validated: as crowd
density rises, YOLO detection saturates then collapses (28→21→15 at 45/100/200 pasted) while CSRNet density
climbs monotonically (61→69→85) — graceful degradation in the dense regime where crush risk lives.
NOTE: CSRNet is trained on street/mall scenes; on the gray sprite-composites it is uncalibrated (blank≈15
offset) — accurate counts need in-domain imagery (real feeds) or DENSITY_SCALE calibration.
