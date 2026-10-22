"""Edge resource profile: run VehicleGPT under the target budget and measure against the constraints.

Budget applied: 2 inference threads (VEHICLEGPT_LLM_THREADS=2), OMP/BLAS threads = 2, and on Linux an address-space cap
(RLIMIT_AS) of --mem-gb (default 2). On macOS the memory cap cannot be enforced by the kernel, so peak RSS is measured
and compared with the budget instead. Results -> benchmarks/edge_profile.json.

  python scripts/edge_profile.py                       # Qwen2.5-1.5B router (default)
  python scripts/edge_profile.py --model models/qwen2.5-0.5b-instruct-q4_k_m.gguf
  python scripts/edge_profile.py --no-llm              # deterministic fallback only
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = [
    "Why has my driving range reduced over the last two weeks?",
    "When did fuel efficiency show the largest drop?",
    "Show all ADAS disengagement events from yesterday.",
    "What happened before the battery overheating warning?",
    "Compare energy consumption between city and highway driving.",
    "Is the car using more juice than it usually does?",
    "Did the lane assist give up on me yesterday?",
    "Which diagnostic warnings occurred repeatedly?",
    "How hot did the motor get on Sep 19?",
    "Show unusual events from yesterday.",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=str(ROOT / "models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"))
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--mem-gb", type=float, default=2.0)
    ap.add_argument("--reps", type=int, default=3)
    a = ap.parse_args()
    os.environ.update({"VEHICLEGPT_LLM_THREADS": "2", "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2",
                       "VEHICLEGPT_NET_PROBE": "0", "VEHICLEGPT_LLM": "none" if a.no_llm else "llamacpp",
                       "VEHICLEGPT_GGUF": a.model})
    cap = "not enforceable on this OS (measured instead)"
    if sys.platform.startswith("linux"):
        import resource
        lim = int(a.mem_gb * 1024 ** 3)
        resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
        cap = f"RLIMIT_AS={a.mem_gb} GB enforced"
    sys.path.insert(0, str(ROOT))
    import psutil
    proc = psutil.Process()
    peak = [0]
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            peak[0] = max(peak[0], proc.memory_info().rss)
            time.sleep(0.02)
    threading.Thread(target=sampler, daemon=True).start()

    t0 = time.perf_counter()
    from vehiclegpt import config, db, llm, runtime
    from vehiclegpt import query_engine as qe
    runtime.ensure_database()
    conn = db.connect()
    t_app = time.perf_counter() - t0
    t = time.perf_counter()
    st = llm.get_adapter(refresh=True).status()
    t_model = time.perf_counter() - t
    rows = []
    for q in QUESTIONS:
        lat = []
        for _ in range(a.reps):
            t = time.perf_counter()
            ans = qe.answer(conn, q, use_llm=not a.no_llm)
            lat.append((time.perf_counter() - t) * 1000)
        rows.append({"q": q, "intent": ans.intent, "routed_by": ans.intent_source, "supported": ans.supported,
                     "median_ms": round(statistics.median(lat)), "max_ms": round(max(lat))})
    t = time.perf_counter()
    ex = qe.add_explanation(qe.answer(conn, QUESTIONS[0], use_llm=not a.no_llm)) if st.available else None
    t_explain = (time.perf_counter() - t) * 1000
    stop.set()
    meds = [r["median_ms"] for r in rows]
    out = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "platform": platform.platform(), "machine": platform.machine(),
           "cpu": platform.processor(), "budget": {"inference_threads": config.LLM_THREADS, "memory_cap": cap, "mem_gb": a.mem_gb},
           "model": Path(a.model).name if not a.no_llm else None, "model_status": st.detail,
           "app_start_s": round(t_app, 2), "model_load_s": round(t_model, 2),
           "query_latency_ms": {"median": statistics.median(meds), "p95": sorted(meds)[int(0.95 * (len(meds) - 1))], "max": max(r["max_ms"] for r in rows)},
           "share_under_2s": sum(r["max_ms"] < 2000 for r in rows) / len(rows),
           "optional_explanation_ms": round(t_explain) if ex else None,
           "peak_rss_mb": round(peak[0] / 1e6), "within_memory_budget": peak[0] < a.mem_gb * 1024 ** 3,
           "per_query": rows}
    name = "edge_profile" + ("_nollm" if a.no_llm else "" if "1.5b" in a.model else "_" + Path(a.model).stem)
    (ROOT / "benchmarks" / f"{name}.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: v for k, v in out.items() if k != "per_query"}, indent=1))
    for r in rows:
        print(f"{r['median_ms']:6} ms  {r['intent']:<18} {r['routed_by']:<10} {r['q']}")


if __name__ == "__main__":
    main()
