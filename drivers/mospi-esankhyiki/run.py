#!/usr/bin/env python3
"""mospi-esankhyiki — generic MoSPI ingest (IIP / WPI / NAS / PLFS / ASI) → stat.series.

Its ONLY I/O is the kernel broker: fetch the platform mospi-gateway (`/series?dataset=…`, already normalized),
write stat.series. Config-driven by DATASET. Idempotent (deterministic id per dataset+indicator+geo+period+dims).
Real official statistics — fetch or fail loudly, NO synthetic fabrication.

  Env: TENANT/WRITE_TENANT · RUN_TOKEN · DATASET (required) · GATEWAY_URL (default http://mospi-gateway:8107)
       YEARS (comma-sep; calendar YYYY for IIP/WPI/PLFS, fiscal YYYY-YY for NAS/ASI; blank ⇒ dataset default)
       INDICATOR_CODES (comma-sep; NAS/PLFS) · AGG (1 = headline only, WPI) · NIC_CODE (ASI, default 99999)
"""
import hashlib, json, os, time, urllib.parse, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT", "")
GATEWAY = os.environ.get("GATEWAY_URL", "http://mospi-gateway:8107").rstrip("/")
DATASET = (os.environ.get("DATASET") or "").upper()
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/mospi-esankhyiki"
SRC_LABEL = "MoSPI eSankhyiki (Ministry of Statistics & Programme Implementation, Govt. of India)"
_Y = int(time.strftime("%Y", time.gmtime()))

# sensible per-dataset defaults so a plain 'Run now' (no env) still ingests a headline time-series
_DEF_YEARS = {"IIP": [str(_Y), str(_Y - 1)], "WPI": [str(_Y), str(_Y - 1)],
              "PLFS": [str(_Y - 1), str(_Y - 2)], "NAS": [],               # NAS: omit year ⇒ API returns its series
              "ASI": ["2023-24", "2022-23"]}                              # ASI: bound to recent fiscal years (per-year fetch)
_DEF_INDICATORS = {"NAS": ["1", "5"], "PLFS": ["1", "2", "3"]}            # NAS: GVA+GDP; PLFS: LFPR+WPR+UR
YEARS = [y.strip() for y in os.environ.get("YEARS", ",".join(_DEF_YEARS.get(DATASET, []))).split(",") if y.strip()]
INDS = [i.strip() for i in os.environ.get("INDICATOR_CODES", ",".join(_DEF_INDICATORS.get(DATASET, []))).split(",") if i.strip()]
AGG = os.environ.get("AGG", "1" if DATASET in ("WPI", "PLFS") else "")     # headline aggregates for the big cross-tab datasets
NIC = os.environ.get("NIC_CODE", "99999" if DATASET == "ASI" else "")


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def gw(params):
    url = f"{GATEWAY}/series?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    r = call("/v1/fetch", {"url": url})
    body = (r or {}).get("body") or ""
    if not body:
        raise RuntimeError(f"empty gateway response (upstream={(r or {}).get('upstream_status')})")
    doc = json.loads(body)
    if not doc.get("ok"):
        raise RuntimeError(f"gateway error: {doc.get('error') or doc.get('msg')}")
    return doc.get("records") or []


import re as _re
_MONTHLY = _re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")   # ONLY real calendar months — NOT fiscal 'YYYY-YY' (e.g. 2019-20)


def obs_at(rec):
    p = rec.get("period") or ""
    if _MONTHLY.match(p):                    # YYYY-MM calendar month
        return p + "-01T00:00:00Z"
    if rec.get("year"):                      # annual / fiscal (e.g. 2019-20) → India fiscal-year start (1 Apr)
        return f"{int(rec['year']):04d}-04-01T00:00:00Z"
    return NOW


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — refusing to guess.")
    if DATASET not in ("IIP", "WPI", "NAS", "PLFS", "ASI"):
        raise SystemExit(f"FATAL: DATASET must be one of IIP/WPI/NAS/PLFS/ASI (got '{DATASET}').")
    year_opts = YEARS or [""]        # [""] ⇒ omit year (dataset returns its latest)
    ind_opts = INDS or [""]
    written, errors, empty = 0, 0, 0
    for ic in ind_opts:
        for yr in year_opts:
            params = {"dataset": DATASET, "year": yr, "indicator_code": ic, "agg": AGG, "nic_code": NIC}
            try:
                recs = gw(params)
            except Exception as e:
                errors += 1
                print(f"[{DATASET}] ic={ic} year={yr} FETCH FAILED: {e}", flush=True)
                continue
            if not recs:
                empty += 1
                print(f"[{DATASET}] ic={ic} year={yr} no data (left empty)", flush=True)
                continue
            for rec in recs:
                if rec.get("value") is None or not rec.get("period"):
                    continue
                dims = rec.get("dims") or {}
                dkey = json.dumps({k: dims[k] for k in sorted(dims)}, ensure_ascii=False)
                key = "|".join([rec.get("dataset", DATASET), str(rec.get("indicator")), str(rec.get("geo_name")),
                                str(rec.get("period")), dkey])
                h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
                gslug = "".join(c for c in (rec.get("geo_name") or "na").lower() if c.isalnum())[:12] or "na"
                oid = f"ds:{TENANT}:stat/series/{DATASET.lower()}-{gslug}-{h}"
                obj = {"schema": "stat.series", "id": oid, "tenant": TENANT,
                       "source": SRC_LABEL, "source_ref": SRC, "observed_at": obs_at(rec), **rec}
                obj = {k: v for k, v in obj.items() if v is not None}
                call("/v1/write", {"scope": "stat.series", "id": oid, "object": obj,
                                   "valid_time": {"from": obj["observed_at"]}, "asserted_at": NOW})
                written += 1
            print(f"[{DATASET}] ic={ic} year={yr}: wrote {len(recs)} records", flush=True)
    print("RESULT: " + json.dumps({"source": "mospi-esankhyiki", "dataset": DATASET,
                                   "written": written, "empty": empty, "errors": errors}))
    if written == 0:
        raise SystemExit(f"FATAL: 0 {DATASET} records written (errors={errors}, empty={empty}).")


if __name__ == "__main__":
    main()
