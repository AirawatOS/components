#!/bin/bash
# Urban services — load the synthetic master data into the staging demo tenants. Run on YOUR machine:
#   bash seed_registries.sh            (ONLY=city-da,uit bash seed_registries.sh to load some tenants)
# The logins file stays on the EC2 host and is piped into the loader inside the network container (sign-in only;
# nothing secret printed). Prints record counts per tenant.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ONLY="${ONLY:-}"
scp -q "$HERE/seed_registries.py" airos:/tmp/seed_registries.py
ssh airos "ONLY=$ONLY bash -s" <<'REMOTE'
set -e
docker cp /tmp/seed_registries.py airawat-network:/tmp/seed_registries.py && rm /tmp/seed_registries.py
docker exec -i -e ONLY="$ONLY" airawat-network python3 /tmp/seed_registries.py < ~/urban-demo-logins.txt
docker exec airawat-network rm -f /tmp/seed_registries.py
REMOTE
