"""Central configuration. All thresholds are DEMONSTRATION values, not manufacturer specifications."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = Path(os.environ.get("VEHICLEGPT_DB", DATA_DIR / "vehiclegpt.db"))
GROUND_TRUTH_PATH = DATA_DIR / "ground_truth.json"

SEED = 42
VEHICLE_ID = "SYN-EV-001"
DATASET_START = "2026-09-01T00:00:00"
DATASET_DAYS = 21
PREHISTORY_DAYS = 35  # extra normal-operation history before DATASET_START (8 weeks total)
SAMPLE_MINUTES = 10
USABLE_BATTERY_KWH = 60.0
NOMINAL_BATTERY_KWH = 64.0

SEVERITIES = ["Informational", "Low", "Medium", "High"]
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# Demonstration thresholds (editable in Settings; persisted in the `settings` table).
DEFAULT_THRESHOLDS: dict[str, float] = {
    "battery_temp_low_c": 40.0,
    "battery_temp_medium_c": 43.0,
    "battery_temp_high_c": 46.0,
    "motor_temp_rate_c_per_10min": 8.0,
    "motor_temp_high_c": 85.0,
    "consumption_low_pct": 10.0,
    "consumption_medium_pct": 20.0,
    "consumption_baseline_days": 7,
    "repeat_dtc_count": 3,
    "repeat_dtc_window_days": 7,
    "gap_minutes": 30.0,
    "battery_temp_plausible_min_c": -40.0,
    "battery_temp_plausible_max_c": 90.0,
    "motor_temp_plausible_max_c": 200.0,
}

# Physical plausibility ranges used by validation (generic, conservative).
PLAUSIBLE = {
    "speed_kmh": (0, 250),
    "soc_pct": (0, 100),
    "ambient_temp_c": (-50, 60),
}

# Local LLM settings
LLM_BACKEND = os.environ.get("VEHICLEGPT_LLM", "auto")  # auto | ollama | llamacpp | none
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("VEHICLEGPT_OLLAMA_MODEL", "qwen2.5:1.5b-instruct-q4_K_M")
GGUF_PATH = os.environ.get("VEHICLEGPT_GGUF", str(ROOT / "models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"))
FALLBACK_GGUF = str(ROOT / "models" / "qwen2.5-0.5b-instruct-q4_k_m.gguf")  # for < 2 GB devices
ROUTER = os.environ.get("VEHICLEGPT_ROUTER", "llm_first")  # llm_first | rules_first
LLM_THREADS = int(os.environ.get("VEHICLEGPT_LLM_THREADS", "2"))   # target: 2 ARM A5x cores
LLM_CTX = int(os.environ.get("VEHICLEGPT_LLM_CTX", "1024"))
LLM_TIMEOUT_S = float(os.environ.get("VEHICLEGPT_LLM_TIMEOUT", "8"))
