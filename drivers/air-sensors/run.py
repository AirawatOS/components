#!/usr/bin/env python3
"""air-sensors driver: AOI (context) → ground-station point observations → air.sensor_reading.

Confined — its only I/O is the kernel broker. It READS the area-of-interest (space.aoi) for the bbox +
resolution, then ingests POINT air-quality readings from the ground networks (CPCB via data.gov.in +
OpenAQ). The reference build SYNTHESIZES a deterministic station field (offline, no vendor key) so the
whole air-quality vertical runs end-to-end; the real fetch path egresses to the declared hosts with the
tenant's broker-injected keys. Point measures carry the ADR-0053 space block {h3,res,centroid}; valid-time
is `observed_at` (the run hour) so downstream layers form a time series.
"""
import json, math, os, time, urllib.request
import h3

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
AOI_ID = os.environ.get("AOI_ID", "ds:kmc:space/aoi/kanpur-core")
OBSERVED_AT = os.environ.get("OBSERVED_AT") or time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime())
N_STATIONS = int(os.environ.get("N_STATIONS", "10"))


def _call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def synth_stations(bbox, n):
    """A deterministic station field over the AOI: a grid of n points with a SW→NE PM2.5 gradient plus one
    hotspot — real spatial structure for the interpolation to smooth. (Real mode: fetch CPCB/OpenAQ.)"""
    w, s, e, nn = bbox
    cols = max(1, int(round(math.sqrt(n)))); rows = max(1, (n + cols - 1) // cols)
    out, k = [], 0
    for r in range(rows):
        for c in range(cols):
            if k >= n:
                break
            lng = w + (e - w) * ((c + 0.5) / cols)
            lat = s + (nn - s) * ((r + 0.5) / rows)
            u = (lng - w) / max(e - w, 1e-9); v = (lat - s) / max(nn - s, 1e-9)
            base = 55.0 + 35.0 * u + 10.0 * v
            hot = 45.0 * math.exp(-(((u - 0.7) ** 2 + (v - 0.3) ** 2) / 0.03))
            out.append({"sensor_id": f"synth-{k+1:02d}", "lat": round(lat, 5), "lng": round(lng, 5),
                        "pm25": round(base + hot, 1), "no2": round(12 + 20 * u, 1), "o3": round(30 + 15 * v, 1)})
            k += 1
    return out


def vendor_probe(bbox):
    """Query the sensor vendor for stations in the AOI THROUGH the broker — a real authenticated egress the
    broker injects the tenant's key into and AUDITS to prov.egress (the key never reaches this code). The
    reference /fetch returns status only, so we still SYNTHESIZE the readings below; but the egress + secret
    injection + audit are REAL — so setting/rotating the key is visible in the Components 'Logs' tab
    (200 = key accepted, 401 = key rejected → the rotate nudge). No key configured → the broker refuses and
    we just synthesize."""
    w, s, e, n = bbox
    url = f"https://api.openaq.org/v3/locations?bbox={w},{s},{e},{n}&limit=100"
    try:
        r = _call("/v1/fetch", {"url": url,
                  "inject": {"name": "OPENAQ_API_KEY", "as": "header", "header": "X-API-Key", "prefix": ""}})
        print(f"  vendor probe: openaq → status {r.get('upstream_status')} (key {r.get('key_used')})")
    except Exception as ex:
        print(f"  vendor probe skipped (no key configured / broker): {ex}")


def main():
    aoi = _call("/v1/read", {"scope": "space.aoi", "id": AOI_ID}).get("object")
    if not aoi:
        print(f"no AOI at {AOI_ID}"); return 1
    bbox, res = aoi["bbox"], aoi["h3_res"]
    vendor_probe(bbox)                                     # real egress + secret + audit (if a key is set)
    stations = synth_stations(bbox, N_STATIONS)
    for st in stations:
        cell = h3.latlng_to_cell(st["lat"], st["lng"], res)
        rid = f"ds:kmc:air/sensor_reading/{st['sensor_id']}-{OBSERVED_AT[:13]}"
        fact = {"schema": "air.sensor_reading", "id": rid, "sensor_id": st["sensor_id"],
                "space": {"h3": cell, "res": res, "centroid": [st["lng"], st["lat"]]},
                "pm25": st["pm25"], "no2": st["no2"], "o3": st["o3"],
                "observed_at": OBSERVED_AT, "source": "synthetic",
                "prov": {"method": "ds:kmc:method/synthetic-stations", "component": "ds:airawat:driver/air-sensors"}}
        _call("/v1/write", {"scope": "air.sensor_reading", "id": rid, "object": fact,
                            "valid_time": {"from": OBSERVED_AT}, "asserted_at": OBSERVED_AT})
    print(f"wrote {len(stations)} air.sensor_reading points for {OBSERVED_AT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
