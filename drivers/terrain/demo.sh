#!/usr/bin/env bash
# Terrain domain-pack slice: seed an AOI (context) → run the terrain driver INSIDE the Component Host
# → verify a geo.terrain layer landed in the registry (one measure per H3 cell).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="$(cd "$HERE/../../../platform-spec/reference" && pwd)"
K=${AIRAWAT_API:-http://localhost:8080}; N=${NETWORK_URL:-http://localhost:8090}
CID="ds:kmc:driver/terrain"; AOI="ds:kmc:space/aoi/kanpur-core"
j() { curl -sf -H 'Content-Type: application/json' "$@"; }

echo "0) certify + publish + install the terrain driver (bound to its code digest)"
DIGEST=$(python3 "$HOST/package.py" "$HERE")
j -X POST "$N/v1/certify" -d "{\"subject_id\":\"$CID\",\"subject_kind\":\"component\",\"evidence_digest\":\"$DIGEST\"}" >/dev/null
j -X POST "$N/v1/store/publish" -d "{\"component_id\":\"$CID\",\"kind\":\"driver\",\"version\":\"0.1.0\",\"provider\":\"ds:airawat:developer\",\"scopes\":[{\"verb\":\"read\",\"scope\":\"space.aoi\"},{\"verb\":\"write\",\"scope\":\"geo.terrain\",\"tenant\":\"kmc\"}]}" >/dev/null
TOK=$(j -X POST "$K/v1/components/install" -d "{\"component_id\":\"$CID\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["installed"]["run_token"])')
echo "   run-token=$TOK"

echo "1) seed the AOI (context an admin authors — the driver's frame)"
j -X POST "$K/v1/registry/objects" -d "{\"id\":\"$AOI\",\"object\":{\"schema\":\"space.aoi\",\"id\":\"$AOI\",\"name\":\"Kanpur core\",\"bbox\":[80.30,26.44,80.38,26.50],\"h3_res\":8}}" >/dev/null
echo "   AOI seeded: Kanpur core bbox=[80.30,26.44,80.38,26.50] res=8"

echo "2) run the terrain driver INSIDE the Component Host (confined; reads AOI, writes cells)"
python3 "$HOST/host.py" "$HERE" --run-token "$TOK" --level L1 --set "AOI_ID=$AOI"

echo "3) verify the geo.terrain layer in the registry"
COUNT=$(j -X POST "$K/v1/registry/resolve" -d '{"filter":{"schema":"geo.terrain"},"limit":1000}' | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["results"]))')
echo "   geo.terrain cells stored: $COUNT"
echo "   sample cell:"
j -X POST "$K/v1/registry/resolve" -d '{"filter":{"schema":"geo.terrain"},"limit":1}' | python3 -c 'import sys,json;o=json.load(sys.stdin)["results"][0]["object"];print("     ",json.dumps({k:o[k] for k in ("id","space","elevation_m","slope_deg","terrain_class")}))'
echo "DONE."
