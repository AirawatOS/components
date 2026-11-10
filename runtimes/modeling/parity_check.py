#!/usr/bin/env python3
"""Modeling-runtime PARITY check: the ML engines train and infer IDENTICALLY on a FIXED input in this image.

Runs the UNMODIFIED engines exactly as the launcher would (a subprocess, RUN_TOKEN + AIRAWAT_API), against a local
FAKE broker that serves one deterministic, seeded feature matrix (Parquet) and records everything the engines write:
  1. engines/pm25-model  PERSIST=1  — XGBoost challenger, leave-station-out CV (scikit-learn GroupKFold), champion gate
  2. engines/pm25-model  INFER=1    — loads the champion, predicts every snapshot cell → air.quality
  3. engines/pm25-apportion         — exact TreeSHAP (XGBoost pred_contribs) → air.source_apportionment + air.source_mix
It prints one FINGERPRINT (sha256 over every write with time stamps removed, plus the serialised model bytes) and a
few headline numbers. Run it in two images; equal fingerprints = same outputs. Exit 1 if an engine fails.
  docker run --rm --network none -v <components>:/drivers:ro <modeling image> python /drivers/runtimes/modeling/parity_check.py
"""
import base64, hashlib, http.server, io, json, os, subprocess, sys, threading

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

COMP = os.environ.get("COMPONENTS", "/drivers")
TENANT = "paritycity"
rng = np.random.default_rng(20261110)

# ── a fixed, seeded input: a labelled station corpus + a grid snapshot ──────────────────────────────────────────────
FEATS = ["pm25_cams", "no2_cams", "so2_cams", "o3_cams", "aod", "road_density", "population", "built_area_m2",
         "pblh_m", "rainfall_mm", "elevation_m", "pm25_cams_lag1", "no2_cams_nbr"]


def frame(n, stations=None, label=True):
    d = {f: rng.normal(50, 15, n).round(3).tolist() for f in FEATS}
    d["pblh_m"] = rng.uniform(200, 1500, n).round(1).tolist()
    d["rainfall_mm"] = rng.exponential(2.0, n).round(2).tolist()
    if label:
        x = np.array([d[f] for f in FEATS]).T
        y = 0.6 * x[:, 0] + 0.3 * x[:, 1] + 0.15 * x[:, 2] - 0.02 * x[:, 8] + rng.normal(0, 4, n)
        d["pm25_obs"] = np.clip(y, 1, 900).round(2).tolist()
        d["station"] = [stations[i % len(stations)] for i in range(n)]
        d["observed_at"] = ["2024-%02d-01T00:00:00Z" % (1 + i % 12) for i in range(n)]
    d["h3_id"] = ["89%013x" % (0x3d7a1 + i) for i in range(n)]
    d["resolution"] = [9] * n
    d["lon"] = (77.5 + rng.uniform(0, 0.3, n)).round(5).tolist()
    d["lat"] = (12.9 + rng.uniform(0, 0.3, n)).round(5).tolist()
    return d


def parquet(d):
    b = io.BytesIO(); pq.write_table(pa.table(d), b); return b.getvalue()


FILES = {"corpus": parquet(frame(720, ["st%02d" % i for i in range(8)])), "snap": parquet(frame(300, label=False))}
MANIFESTS = [{"id": "ds:%s:air/feature_matrix/train" % TENANT, "feature_set": "pm25-train-cpcb", "file_id": "corpus",
              "format": "parquet", "feature_cols": FEATS, "observed_at": "2025-01-01T00:00:00Z"},
             {"id": "ds:%s:air/feature_matrix/snap" % TENANT, "feature_set": "pm25", "file_id": "snap",
              "format": "parquet", "feature_cols": FEATS, "observed_at": "2025-01-15T06:00:00Z"}]
WRITES = []          # every write, in order
STORE = {}           # scope -> {id: object}  (what /v1/query returns)


class Broker(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        p = self.path
        if p == "/v1/query":
            sc = body.get("scope")
            rows = MANIFESTS if sc == "air.feature_matrix" else list(STORE.get(sc, {}).values())
            out = {"results": [{"object": o} for o in rows]}
        elif p == "/v1/file/get":
            out = {"content_b64": base64.b64encode(FILES.get(body.get("id"), b"")).decode()}
        elif p == "/v1/file/put":
            raw = base64.b64decode(body.get("content_b64") or "")
            fid = "sha2-" + hashlib.sha256(raw).hexdigest()
            FILES[fid] = raw; WRITES.append({"file_put": fid}); out = {"id": fid}
        elif p in ("/v1/write", "/v1/write/batch"):
            items = body.get("items") or [body]
            for it in items:
                STORE.setdefault(it["scope"], {})[it["id"]] = it["object"]; WRITES.append({"write": it})
            out = {"written": len(items), "error_count": 0}
        else:
            out = {"error": "unknown path " + p}
        b = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Broker)
threading.Thread(target=srv.serve_forever, daemon=True).start()
API = "http://127.0.0.1:%d" % srv.server_address[1]


def run(engine, **env):
    e = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": "/tmp", "RUN_TOKEN": "parity",
         "AIRAWAT_API": API, "TENANT": TENANT, **{k: str(v) for k, v in env.items()}}
    r = subprocess.run([sys.executable, os.path.join(COMP, "engines", engine, "run.py")], env=e, capture_output=True, text=True, timeout=1200)
    res = [l for l in r.stdout.splitlines() if l.startswith("RESULT:")]
    print("  %-34s exit %d  %s" % (engine + (" " + " ".join("%s=%s" % kv for kv in env.items()) if env else ""), r.returncode,
                                    (res[-1][:150] if res else (r.stdout + r.stderr).strip().splitlines()[-1:])), flush=True)
    return r.returncode


rc = run("pm25-model", PERSIST=1, FEATURE_SET="pm25-train-cpcb", SNAPSHOT_FEATURE_SET="pm25", CV_FOLDS=3, MIN_R2=-10)
rc |= run("pm25-model", INFER=1, FEATURE_SET="pm25-train-cpcb", SNAPSHOT_FEATURE_SET="pm25", TRAIN_IF_MISSING=0)
rc |= run("pm25-apportion", FEATURE_SET="pm25-train-cpcb", SNAPSHOT_FEATURE_SET="pm25")

TIME_KEYS = {"observed_at", "asserted_at", "built_at", "valid_time"}


def strip(o):
    if isinstance(o, dict):
        return {k: strip(v) for k, v in sorted(o.items()) if k not in TIME_KEYS}
    if isinstance(o, list):
        return [strip(v) for v in o]
    return o


norm = []
for w in WRITES:
    if "write" in w:
        it = w["write"]
        oid = it["id"].split("/challenger/")[0]          # the challenger id embeds the run time
        norm.append({"scope": it["scope"], "id": oid, "object": strip(it["object"])})
    else:
        norm.append({"file": hashlib.sha256(FILES[w["file_put"]]).hexdigest()})
h = hashlib.sha256(json.dumps(norm, sort_keys=True).encode()).hexdigest()
q = [w["write"]["object"]["pm25"] for w in WRITES if "write" in w and w["write"]["scope"] == "air.quality"]
model = [w["write"]["object"] for w in WRITES if "write" in w and w["write"]["scope"] == "air.model"]
import xgboost, sklearn
print("versions: xgboost %s (%s), scikit-learn %s, numpy %s" % (xgboost.__version__,
      "xgboost-cpu" if not __import__("importlib.util").util.find_spec("nvidia") else "xgboost + nvidia", sklearn.__version__, np.__version__))
print("headline: model r2(LSO)=%s · %d air.quality cells, mean pm25 %.3f · %d writes" % (
      (model[0].get("metrics") or {}).get("leave_station_out", {}).get("r2") if model else None, len(q),
      (sum(q) / len(q)) if q else float("nan"), len(WRITES)))
print("FINGERPRINT " + h)
sys.exit(1 if rc else 0)
