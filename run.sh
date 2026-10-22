#!/usr/bin/env bash
# Launch VehicleGPT 2.0 (creates/migrates the local database and demo accounts on first run).
cd "$(dirname "$0")"
exec .venv/bin/python -m streamlit run app.py --server.port "${PORT:-8502}" "$@"
