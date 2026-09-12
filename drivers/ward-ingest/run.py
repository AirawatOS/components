#!/usr/bin/env python3
"""ward-ingest — parse an uploaded WARD BOUNDARY GeoJSON → space.ward (City Setup).

The operator uploads the city's official ward/prabhag GIS (GeoJSON FeatureCollection) at City Setup; it is stored
as a component asset whose file_id is injected as WARD_GEOJSON. This driver fetches it via the broker, parses each
feature into a governed space.ward (ward_no + name + geometry + centroid), and writes it. ward-h3-map then maps
cells to wards. REAL uploaded boundaries only — fails loudly if no file; NO synthetic wards.

  Env: TENANT/WRITE_TENANT · RUN_TOKEN · WARD_GEOJSON (file_id, injected from the uploaded asset)
       SOURCE (attribution label) · WARD_NO_KEY / WARD_NAME_KEY (override the property keys if non-standard)
"""
import base64, json, os, time, urllib.request

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
TENANT = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT", "")
FID = os.environ.get("WARD_GEOJSON", "").strip()
SOURCE = os.environ.get("SOURCE", "uploaded ward boundary")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
NO_KEYS = [k for k in [os.environ.get("WARD_NO_KEY")] if k] + ["ward_no", "ward", "WARD", "WARD_NO", "prabhag", "PRABHAG", "no", "number", "ID", "id"]
NM_KEYS = [k for k in [os.environ.get("WARD_NAME_KEY")] if k] + ["name", "NAME", "ward_name", "WARD_NAME", "prabhag_name", "label"]


def call(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=90).read())


def fetch_file(fid):
    r = call("/v1/file/get", {"id": fid})
    for k in ("body", "content", "text"):
        if isinstance(r, dict) and r.get(k):
            return r[k]
    for k in ("body_b64", "content_b64", "data_b64"):
        if isinstance(r, dict) and r.get(k):
            return base64.b64decode(r[k]).decode("utf-8", "replace")
    if isinstance(r, dict) and r.get("url"):
        return urllib.request.urlopen(r["url"], timeout=60).read().decode("utf-8", "replace")
    raise SystemExit("FATAL: could not read uploaded file %s (unexpected /v1/file/get shape: %s)" % (fid, list(r.keys()) if isinstance(r, dict) else type(r)))


def pick(props, keys):
    for k in keys:
        if k in props and props[k] not in (None, ""):
            return props[k]
    return None


def centroid(geom):
    xs, ys = [], []
    def walk(a):
        if a and isinstance(a[0], (int, float)):
            xs.append(a[0]); ys.append(a[1])
        else:
            for b in a: walk(b)
    walk((geom or {}).get("coordinates") or [])
    return [sum(xs) / len(xs), sum(ys) / len(ys)] if xs else None


def main():
    if not TENANT:
        raise SystemExit("FATAL: TENANT not set — refusing to guess.")
    if not FID:
        raise SystemExit("FATAL: WARD_GEOJSON not set — upload a ward boundary GeoJSON at City Setup (bound as the WARD_GEOJSON asset).")
    doc = json.loads(fetch_file(FID))
    feats = doc.get("features") if isinstance(doc, dict) else (doc if isinstance(doc, list) else [])
    if not feats:
        raise SystemExit("FATAL: no features in the uploaded GeoJSON — expected a FeatureCollection of ward polygons.")
    written = 0
    for i, f in enumerate(feats):
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        wno = pick(props, NO_KEYS) or str(i + 1)
        name = pick(props, NM_KEYS)
        wno = str(wno)
        wid = "".join(ch for ch in wno if ch.isalnum()) or str(i + 1)
        oid = f"ds:{TENANT}:space/ward/{wid}"
        fact = {"schema": "space.ward", "id": oid, "tenant": TENANT, "ward_no": wno,
                "name": (str(name)[:120] if name else None), "jurisdiction": TENANT, "geometry": geom,
                "centroid": centroid(geom), "source": SOURCE, "observed_at": NOW}
        call("/v1/write", {"scope": "space.ward", "id": oid, "object": {k: v for k, v in fact.items() if v is not None},
                           "valid_time": {"from": NOW}, "asserted_at": NOW})
        written += 1
    print("wrote %d space.ward from the uploaded boundary" % written, flush=True)
    print("RESULT: " + json.dumps({"source": "uploaded-geojson", "wards": written}))
    if written == 0:
        raise SystemExit("FATAL: 0 ward polygons parsed — check the file has Polygon/MultiPolygon features.")


if __name__ == "__main__":
    main()
