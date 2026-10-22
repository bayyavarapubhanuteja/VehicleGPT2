"""One-time, ONLINE step: download the quantised models into ./models (the app itself never goes online).
  python scripts/fetch_models.py            # default router model (Qwen2.5-1.5B-Instruct Q4_K_M, ~1.1 GB) + 0.5B fallback
  python scripts/fetch_models.py --all      # also Llama-3.2-1B for the comparison benchmark
"""
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download

ROOT = Path(__file__).resolve().parent.parent
MODELS = [("Qwen/Qwen2.5-1.5B-Instruct-GGUF", "qwen2.5-1.5b-instruct-q4_k_m.gguf"),
          ("Qwen/Qwen2.5-0.5B-Instruct-GGUF", "qwen2.5-0.5b-instruct-q4_k_m.gguf")]
if "--all" in sys.argv:
    MODELS.append(("bartowski/Llama-3.2-1B-Instruct-GGUF", "Llama-3.2-1B-Instruct-Q4_K_M.gguf"))
for repo, f in MODELS:
    print(hf_hub_download(repo, f, local_dir=ROOT / "models"))
