#!/usr/bin/env python3
"""satellite-lst driver: fetches Land Surface Temperature (LST) observation data
from a satellite/remote-sensing data source and writes it as a environment.lst_observation
governed object.

Confined — its only I/O is the kernel broker. The driver fetches a satellite LST product
(e.g. MODIS-derived LST) for a given location/time window via the broker's egress-gated
/v1/fetch, parses the response, and persists the reading as a governed knowledge object
so downstream components can query it.

Inputs (env):
  LST_SOURCE_URL   URL of the satellite LST data endpoint/product to fetch (required)
  LAT, LON         latitude/longitude of the observation point (required, floats)
  OBS_DATE         observation date (YYYY-MM-DD); defaults to fetch time if absent
  SATELLITE_NAME   name of the satellite/sensor (default: MODIS)
  DOC_ID           optional deterministic key override for the written object id
"""
import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

AIRAWAT_API = os.environ["AIRAWAT_API"]
RUN_TOKEN = os.environ["RUN_TOKEN"]


def call(path, body):
    req = urllib.request.Request(
        AIRAWAT_API + path,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Run-Token": RUN_TOKEN,
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        raw = resp.read().decode("utf-8")
    if not raw:
        return {}
    return json.loads(raw)


def fail(msg):
    sys.stderr.write(f"satellite-lst driver error: {msg}\n")
    sys.exit(1)


def deterministic_key(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


def main():
    lst_source_url = os.environ.get("LST_SOURCE_URL")
    if not lst_source_url:
        fail("LST_SOURCE_URL is required")

    lat_raw = os.environ.get("LAT")
    lon_raw = os.environ.get("LON")
    if lat_raw is None or lon_raw is None:
        fail("LAT and LON are required")

    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except ValueError:
        fail("LAT and LON must be numeric")

    obs_date = os.environ.get("OBS_DATE")
    satellite_name = os.environ.get("SATELLITE_NAME", "MODIS")
    doc_id_override = os.environ.get("DOC_ID")

    ctx = call("/v1/context", {})
    tenant = ctx.get("tenant")
    if not tenant:
        fail("could not determine tenant from /v1/context")

    fetch_resp = call(
        "/v1/fetch",
        {
            "url": lst_source_url,
            "method": "GET",
            "body": None,
        },
    )

    raw_body = fetch_resp.get("body")
    if raw_body is None:
        fail("fetch response missing 'body' key")

    lst_value = None
    lst_unit = "K"
    parsed = None

    if isinstance(raw_body, (dict, list)):
        parsed = raw_body
    else:
        try:
            parsed = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            parsed = None

    if isinstance(parsed, dict):
        for key in ("lst", "LST", "land_surface_temperature", "value", "temperature_k"):
            if key in parsed:
                try:
                    lst_value = float(parsed[key])
                except (TypeError, ValueError):
                    lst_value = None
                break
        if "unit" in parsed and isinstance(parsed["unit"], str):
            lst_unit = parsed["unit"]
        if obs_date is None:
            for key in ("date", "obs_date", "acquisition_date", "timestamp"):
                if key in parsed:
                    obs_date = str(parsed[key])
                    break
    else:
        try:
            lst_value = float(str(raw_body).strip())
        except ValueError:
            lst_value = None

    if lst_value is None:
        fail("could not parse LST value from fetched response")

    if not obs_date:
        obs_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    retrieved_at = datetime.now(timezone.utc).isoformat()

    schema = "environment.lst_observation"
    scope = schema

    key = doc_id_override or deterministic_key(
        satellite_name, f"{lat:.6f}", f"{lon:.6f}", obs_date, lst_source_url
    )

    object_id = f"ds:{tenant}:" + schema.replace(".", "/") + f"/{key}"

    obj = {
        "schema": schema,
        "id": object_id,
        "tenant": tenant,
        "satellite_name": satellite_name,
        "latitude": lat,
        "longitude": l
