#!/usr/bin/env bash
# Raspberry Pi 4/5 (64-bit Raspberry Pi OS / Debian 12) or Graviton (Ubuntu 22.04+) native setup.
set -euo pipefail
cd "$(dirname "$0")/.."
sudo apt-get update && sudo apt-get install -y python3-venv python3-dev build-essential cmake can-utils
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt python-can cantools
# llama.cpp with ARM NEON (and dot-product on A76/Graviton) — built from source for the local CPU:
CMAKE_ARGS="-DGGML_NATIVE=ON" .venv/bin/python -m pip install "llama-cpp-python==0.3.2"
# Optional SocketCAN on a real bus (MCP2515/MCP2518FD HAT): listen-only so VehicleGPT can never transmit
# sudo ip link set can0 type can bitrate 500000 dbitrate 2000000 fd on listen-only on && sudo ip link set can0 up
mkdir -p models
echo "Copy models/qwen2.5-1.5b-instruct-q4_k_m.gguf (or the 0.5B fallback) into ./models, then:"
echo "  taskset -c 0,1 .venv/bin/python scripts/edge_profile.py      # pin to 2 cores and measure"
echo "  ./run.sh"
