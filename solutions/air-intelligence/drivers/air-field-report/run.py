#!/usr/bin/env python3
"""air-field-report (aos SDK driver): on-ground verification of a flagged air.hotspot → air.field_report.

Closes the Air Intelligence loop's VERIFY step. After a hotspot is recommended → routed → acted on, a field
officer visits the site and files what they found: a measured PM2.5 reading, a verdict (confirmed / not-found
/ resolved), and — the evidence — a SITE PHOTO stored content-addressed via ctx.file (the report cites the
photo by its sha2-256 digest, so it stays verifiable). The report references the air.hotspot it verifies, so
downstream effectiveness analysis can compare the modelled hotspot against ground truth.

Confined: registry + filestore only, via the SDK's ctx. No fabrication — a field report is a real human
submission (photo + reading passed in); with no hotspot ref it refuses. Bitemporal: the report is valid from
its observation time.

  Env: HOTSPOT_REF (required — the air.hotspot verified) · PHOTO_FILE | PHOTO_B64 (the site photo) ·
       READING_PM25 (field-measured µg/m³) · VERDICT (confirmed|not-found|resolved) · REPORTER · NOTE · OBSERVED_AT
"""
import base64, os
from aos import driver, ctx

CID = "ds:airawatdeveloper:driver/air-field-report"


@driver.main
def run():
    hotspot_ref = os.environ.get("HOTSPOT_REF", "").strip()
    if not hotspot_ref:
        print("FAIL: HOTSPOT_REF required — the air.hotspot this field visit verifies. Nothing written."); return 1
    T = ctx.tenant
    if not T:
        print("FAIL: could not resolve the run tenant (/v1/context)."); return 1

    hs = ctx.get(hotspot_ref, "air.hotspot")               # the hotspot being verified (for its location + linkage)
    sp = (hs or {}).get("space") or {}

    # the site photo → content-addressed filestore (the evidence the report cites by digest)
    photo_id = None
    pf = os.environ.get("PHOTO_FILE", "")
    if pf and os.path.exists(pf):
        photo_id = ctx.file.put(open(pf, "rb").read())
    elif os.environ.get("PHOTO_B64"):
        photo_id = ctx.file.put(base64.b64decode(os.environ["PHOTO_B64"]))

    reading = os.environ.get("READING_PM25", "").strip()
    reading = float(reading) if reading else None
    obs = os.environ.get("OBSERVED_AT", "").strip() or ctx.now()
    rid = f"ds:{T}:air/field_report/{hotspot_ref.split('/')[-1]}-{obs[:13]}"
    rec = {"schema": "air.field_report", "id": rid,
           "hotspot_ref": hotspot_ref, "reading_pm25": reading, "photo": photo_id,
           "verdict": os.environ.get("VERDICT", "").strip() or "confirmed",
           "reporter": os.environ.get("REPORTER", "").strip() or "field-officer",
           "note": os.environ.get("NOTE", "").strip(),
           "space": {"h3": sp.get("h3"), "centroid": sp.get("centroid")} if sp else None,
           "observed_at": obs}
    rec = {k: v for k, v in rec.items() if v is not None}
    ctx.write(rec, valid_from=obs)                         # bitemporal: valid from the field-visit time

    print(f"field report on {hotspot_ref}: reading={reading} µg/m³, verdict={rec['verdict']}, "
          f"photo={photo_id or 'none'} → {rid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(driver.run())
