#!/usr/bin/env bash
# Run VehicleGPT with the target resource budget: 2 CPU cores, 2 GB RAM, no network except the UI port.
# Usage: deploy/run_edge.sh [profile|app]
set -euo pipefail
cd "$(dirname "$0")/.."
MODE=${1:-app}
COMMON=(--platform linux/arm64 --cpus 2 --memory 2g --memory-swap 2g -v "$PWD/models:/opt/vehiclegpt/models:ro"
        -v "$PWD/data:/opt/vehiclegpt/data")
if [ "$MODE" = profile ]; then
  # benchmark inside the constrained container (no network at all)
  docker run --rm --network none "${COMMON[@]}" -e VEHICLEGPT_ENV=development vehiclegpt:edge \
    python scripts/edge_profile.py --mem-gb 2
else
  docker run --rm -p 8502:8502 "${COMMON[@]}" vehiclegpt:edge
fi
