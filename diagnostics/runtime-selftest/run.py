import os, json, urllib.request
import h3  # proves the geospatial runtime image provides the lib (not the host python)
tok = os.environ["RUN_TOKEN"]; api = os.environ["AIRAWAT_API"]
req = urllib.request.Request(api + "/v1/context", headers={"X-Run-Token": tok})
ctx = json.loads(urllib.request.urlopen(req, timeout=15).read())
res = h3.cell_to_latlng(h3.latlng_to_cell(19.0, 72.8, 9))
print(f"runtime-selftest OK: h3 works ({res[0]:.3f},{res[1]:.3f}) | broker reachable | run_id={ctx.get('run_id')}")
