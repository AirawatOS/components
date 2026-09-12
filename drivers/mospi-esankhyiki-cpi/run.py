#!/usr/bin/env python3
"""mospi-esankhyiki-cpi — ingest India's official Consumer Price Index (CPI) → stat.price_index.

Its ONLY I/O is the kernel broker:
  fetch  the platform mospi-gateway (which fronts MoSPI's eSankhyiki open API + handles its legacy-TLS quirk)
  write  stat.price_index — one governed row per (base_year, series, geography, sector, group, subgroup, month)

For each (state_code × year) it asks the gateway for normalized CPI records and writes each as a governed,
attributed statistical time-series point. Idempotent: the id is a deterministic hash of the dimensions, so a
re-run upserts the same rows (and revised/Final values overwrite earlier Provisional ones).

REAL official statistics only — fetch or fail loudly. NO synthetic fabrication, NO tenant default. An empty
upstream month is left empty, never invented. If EVERY (state,year) fetch fails, the run exits non-zero.

  Env: TENANT/WRITE_TENANT (required) · RUN_TOKEN
       GATEWAY_URL (default http://mospi-gateway:8107) · BASE_YEAR (2012) · SERIES (Current)
       STATE_CODES ("99") · YEARS (default two latest calendar years) · GROUP_CODES · SECTOR_CODES
"""
import hashlib, json, os, re, time, urllib.parse, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT", "")
GATEWAY = os.environ.get("GATEWAY_URL", "http://mospi-gateway:8107").rstrip("/")
BASE_YEAR = os.environ.get("BASE_YEAR", "2012")
SERIES = os.environ.get("SERIES", "Current")
STATE_CODES = [s.strip() for s in os.environ.get("STATE_CODES", "99").split(",") if s.strip()]
GROUP_CODES = os.environ.get("GROUP_CODES", "").strip()
SECTOR_CODES = os.environ.get("SECTOR_CODES", "").strip()
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SRC = "ds:airawat:ref/source/mospi-esankhyiki"
SRC_LABEL = "MoSPI eSankhyiki (Ministry of Statistics & Programme Implementation, Govt. of India)"

# Default to the two most recent calendar years (UTC) so a scheduled run always covers the latest release.
_Y = int(time.strftime("%Y", time.gmtime()))
YEARS = [y.strip() for y in os.environ.get("YEARS", f"{_Y},{_Y-1}").split(",") if y.strip()]


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def gw_cpi(state_code, year):
    """Fetch normalized CPI records from the gateway via the broker (egress-gated to mospi-gateway)."""
    q = {"base_year": BASE_YEAR, "series": SERIES, "state_code": state_code, "year": year, "limit": "5000"}
    if GROUP_CODES:
        q["group_code"] = GROUP_CODES
    if SECTOR_CODES:
        q["sector_code"] = SECTOR_CODES
    url = f"{GATEWAY}/cpi?" + urllib.parse.urlencode(q)
    r = call("/v1/fetch", {"url": url})
    body = (r or {}).get("body") or ""
    if not body:
        raise RuntimeError(f"empty gateway response (upstream_status={(r or {}).get('upstream_status')})")
    doc = json.loads(body)
    if not doc.get("ok"):
        raise RuntimeError(f"gateway error: {doc.get('error') or doc.get('msg')}")
    return doc.get("records") or []


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "na"


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — refusing to guess.")
    written, errors, empty = 0, 0, 0
    for sc in STATE_CODES:
        for yr in YEARS:
            try:
                recs = gw_cpi(sc, yr)
            except Exception as e:
                errors += 1
                print(f"[cpi] state={sc} year={yr} FETCH FAILED: {e}", flush=True)
                continue
            if not recs:
                empty += 1
                print(f"[cpi] state={sc} year={yr} no data (left empty)", flush=True)
                continue
            for rec in recs:
                if rec.get("index") is None or not rec.get("period"):
                    continue
                key = "|".join([rec.get("indicator", "CPI"), rec.get("base_year", BASE_YEAR),
                                rec.get("series", SERIES), str(rec.get("geo_name")), str(rec.get("sector")),
                                str(rec.get("group")), str(rec.get("subgroup")), rec["period"]])
                h = hashlib.sha1(key.encode()).hexdigest()[:16]
                oid = f"ds:{TENANT}:stat/price_index/{_slug(rec.get('indicator','CPI'))}-{rec['base_year']}-{_slug(rec.get('geo_name'))}-{_slug(rec.get('sector'))}-{h}"
                obs = rec["period"] + "-01T00:00:00Z"
                obj = {"schema": "stat.price_index", "id": oid, "tenant": TENANT,
                       "source": SRC_LABEL, "source_ref": SRC, "observed_at": obs, **rec}
                obj = {k: v for k, v in obj.items() if v is not None}
                call("/v1/write", {"scope": "stat.price_index", "id": oid, "object": obj,
                                   "valid_time": {"from": obs}, "asserted_at": NOW})
                written += 1
            print(f"[cpi] state={sc} year={yr}: wrote {len(recs)} records", flush=True)
    print("RESULT: " + json.dumps({"source": "mospi-esankhyiki", "indicator": "CPI",
                                   "base_year": BASE_YEAR, "written": written, "empty": empty, "errors": errors}))
    if written == 0:
        raise SystemExit(f"FATAL: 0 CPI records written (errors={errors}, empty={empty}) — check the gateway / params.")


if __name__ == "__main__":
    main()
