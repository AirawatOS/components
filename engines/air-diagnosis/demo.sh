#!/usr/bin/env bash
# End-to-end: certify → publish → install → seed a fact → run the CONFINED engine → verify it climbed
# the ladder (descriptive + prescriptive Analyses + a Signal) — all through the broker, all attributed.
set -euo pipefail
K=${AIRAWAT_API:-http://localhost:8080}; N=${NETWORK_URL:-http://localhost:8090}
CID="ds:kmc:engine/air-diagnosis"; STATION="ds:kmc:assertion/air/station-7/today"
j() { curl -sf -H 'Content-Type: application/json' "$@"; }

echo "1) certify + publish the engine (with read+write scopes) to the network store"
j -X POST "$N/v1/certify" -d "{\"subject_id\":\"$CID\",\"subject_kind\":\"component\",\"evidence\":{\"conformance\":\"INT-0001 analysis-producer + signal-publisher\"}}" >/dev/null
j -X POST "$N/v1/store/publish" -d "{\"component_id\":\"$CID\",\"kind\":\"engine\",\"version\":\"0.1.0\",\"provider\":\"ds:airawat:developer\",\"scopes\":[{\"verb\":\"read\",\"scope\":\"air.sensor_reading\"},{\"verb\":\"write\",\"scope\":\"intelligence.analysis\",\"tenant\":\"kmc\"},{\"verb\":\"write\",\"scope\":\"intelligence.signal\",\"tenant\":\"kmc\"}]}" >/dev/null
echo "   published + certified."

echo "2) install into the kernel (cert verified point-of-use) → mints the engine run-token"
TOK=$(j -X POST "$K/v1/components/install" -d "{\"component_id\":\"$CID\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["installed"]["run_token"])')
echo "   run-token=$TOK"

echo "3) seed an unhealthy pm2.5 reading (a driver writes the fact the engine will read)"
j -X POST "$K/v1/write" -H "X-Run-Token: $TOK" \
  -d "{\"scope\":\"air.sensor_reading\",\"id\":\"$STATION\",\"object\":{\"kind\":\"assertion\",\"pollutant\":\"pm25\",\"station\":\"station-7\",\"value\":82},\"valid_time\":{\"from\":\"2025-05-12T00:00:00Z\"},\"asserted_at\":\"2025-05-12T06:00:00Z\"}" >/dev/null
echo "   seeded pm25=82 at $STATION"

echo "4) run the CONFINED engine (its only I/O is the broker)"
RUN_TOKEN="$TOK" STATION="$STATION" AIRAWAT_API="$K" python3 components/engines/air-diagnosis/run.py

echo "5) verify the Analyses + Signal landed in the registry"
for id in "ds:kmc:analysis/air/today/descriptive" "ds:kmc:analysis/air/today/prescriptive" "ds:kmc:signal/air/today"; do
  echo -n "   $id → "; j -X POST "$K/v1/registry/resolve" -d "{\"id\":\"$id\"}" | python3 -c 'import sys,json;r=json.load(sys.stdin)["results"];print(json.dumps(r[0]["object"].get("analysis_type") or r[0]["object"].get("signal_type")) if r else "MISSING")'
done
echo "DONE."
