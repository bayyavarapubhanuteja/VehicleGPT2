"""Benchmark VehicleGPT on this machine and write benchmarks/results.json + results.md.

Measures: cold start (dataset generation + detection), DB query time, query latency
(deterministic path and, if available, LLM path), peak memory, model init time,
and alert-detection precision/recall/F1 on labelled synthetic events across seeds.
"""
from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vehiclegpt import analytics, anomaly, config, data_gen, db, llm, runtime  # noqa: E402
from vehiclegpt import query_engine as qe  # noqa: E402

QUERIES = [
    "Why did my driving range decrease over the last seven days?",
    "What happened before the battery temperature warning?",
    "Show unusual events from yesterday.",
    "Compare this week's energy consumption with last week.",
    "Which diagnostic warnings occurred repeatedly?",
    "What was the highest motor temperature on Sep 19?",
    "What was the average battery temperature last 3 days?",
    "How far did I drive last week?",
    "What is the brake pad wear?",
]
EXPECTED_INTENTS = ["range_change", "before_warning", "unusual_events", "compare_energy", "repeated_dtc",
                    "metric_stat", "metric_stat", "distance", "unsupported"]


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def main(reps: int = 20):
    tmp = Path(tempfile.mkdtemp()) / "bench.db"
    out: dict = {"timestamp": datetime.now().isoformat(timespec="seconds"),
                 "conditions": {"python": sys.version.split()[0], "platform": platform.platform(),
                                "machine": platform.machine(), "processor": platform.processor(),
                                "cpu_count": os.cpu_count(), "reps_per_query": reps}}
    rss0 = runtime.process_rss_mb()
    tracemalloc.start()
    t = time.perf_counter()
    info = data_gen.generate(tmp)
    t_gen = (time.perf_counter() - t) * 1000
    conn = db.connect(tmp)
    t = time.perf_counter()
    alerts = anomaly.run_detection(conn)
    t_det = (time.perf_counter() - t) * 1000
    out["cold_start_ms"] = {"dataset_generation": round(t_gen, 1), "detection": round(t_det, 1)}
    out["dataset"] = info

    # DB query timings
    p7 = analytics.last_n_days(conn, 7)
    dbt = []
    for _ in range(reps):
        t = time.perf_counter(); db.read_telemetry(conn, p7.s, p7.e); dbt.append((time.perf_counter() - t) * 1000)
    out["db_query_7d_ms"] = {"median": round(statistics.median(dbt), 2), "p95": round(pct(dbt, 95), 2)}

    # Deterministic query latency + accuracy
    lat, correct = {}, 0
    for q, exp in zip(QUERIES, EXPECTED_INTENTS):
        xs = []
        for _ in range(reps):
            t = time.perf_counter(); a = qe.answer(conn, q, use_llm=False); xs.append((time.perf_counter() - t) * 1000)
        correct += a.intent == exp
        lat[q] = {"median_ms": round(statistics.median(xs), 1), "p95_ms": round(pct(xs, 95), 1), "intent": a.intent,
                  "expected": exp}
    allx = [v["median_ms"] for v in lat.values()]
    out["query_latency_deterministic"] = {"per_query": lat, "median_of_medians_ms": round(statistics.median(allx), 1),
                                          "max_median_ms": max(allx), "intent_accuracy": f"{correct}/{len(QUERIES)}"}
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    out["memory"] = {"python_heap_peak_mb": round(peak / 1e6, 1), "process_rss_start_mb": round(rss0 or 0, 1),
                     "process_rss_end_mb": round(runtime.process_rss_mb() or 0, 1)}

    # LLM
    t = time.perf_counter()
    ad = llm.get_adapter(refresh=True)
    st = ad.status()
    out["llm"] = {"backend": st.backend, "model": st.model, "available": st.available, "detail": st.detail,
                  "adapter_init_ms": round((time.perf_counter() - t) * 1000, 1)}
    if st.available:
        xs = []
        for q in QUERIES[:5]:
            t = time.perf_counter(); a = qe.answer(conn, q, use_llm=True); xs.append((time.perf_counter() - t) * 1000)
        out["llm"]["query_latency_with_llm_ms"] = {"median": round(statistics.median(xs), 1), "max": round(max(xs), 1)}
        out["llm"]["process_rss_after_mb"] = round(runtime.process_rss_mb() or 0, 1)
    else:
        out["llm"]["note"] = "No local model available on this machine — LLM latency/memory NOT measured."

    # Detection quality across seeds (scenario timing fixed; noise varies by seed)
    gt = data_gen.ground_truth()
    evals = []
    for seed in [42, 1, 2, 3, 4, 5]:
        p = Path(tempfile.mkdtemp()) / f"s{seed}.db"
        data_gen.generate(p, seed=seed)
        with db.session(p) as c:
            ev = anomaly.evaluate(anomaly.run_detection(c, persist=False), gt)
        evals.append({"seed": seed, **{k: ev[k] for k in ("precision", "recall", "f1", "alerts_scored", "missed")},
                      "false_positives": len(ev["false_positives"])})
    out["detection_eval"] = {"per_seed": evals,
                             "mean_precision": round(statistics.mean(e["precision"] for e in evals), 3),
                             "mean_recall": round(statistics.mean(e["recall"] for e in evals), 3),
                             "mean_f1": round(statistics.mean(e["f1"] for e in evals), 3),
                             "caveat": "Rules were designed with knowledge of the injected scenarios (seed 42); other seeds change "
                                       "noise only. Scores indicate rule correctness on synthetic data, not real-world accuracy."}
    with db.session(tmp) as c:
        fe = anomaly.evaluate(anomaly.run_detection(c, persist=False), gt, include_fleet=True)
    out["fleet_eval"] = {k: fe[k] for k in ("precision", "recall", "f1", "missed", "events", "alerts_scored")}
    import subprocess
    t = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:warnings", str(ROOT / "tests")],
                       capture_output=True, text=True, cwd=ROOT)
    out["tests"] = (t.stdout.strip().splitlines() or ["not run"])[-1]
    targets = {"<2 s common-query latency (deterministic path)": out["query_latency_deterministic"]["max_median_ms"] < 2000,
               "~2 GB RAM (process RSS, no LLM loaded)": (out["memory"]["process_rss_end_mb"] or 0) < 2048}
    out["targets_measured"] = targets
    (ROOT / "benchmarks" / "results.json").write_text(json.dumps(out, indent=2))
    md = [f"# Benchmark results ({out['timestamp']})", "", f"Conditions: `{json.dumps(out['conditions'])}`", "",
          f"- Cold start: dataset generation {t_gen:.0f} ms, detection {t_det:.0f} ms",
          f"- DB 7-day telemetry query: median {out['db_query_7d_ms']['median']} ms",
          f"- Query latency (deterministic): median-of-medians {out['query_latency_deterministic']['median_of_medians_ms']} ms, "
          f"worst median {out['query_latency_deterministic']['max_median_ms']} ms; intent accuracy "
          f"{out['query_latency_deterministic']['intent_accuracy']}",
          f"- Memory: Python heap peak {out['memory']['python_heap_peak_mb']} MB, process RSS {out['memory']['process_rss_end_mb']} MB",
          f"- LLM: {st.backend} — {st.detail}",
          f"- Detection (6 seeds): P={out['detection_eval']['mean_precision']} R={out['detection_eval']['mean_recall']} "
          f"F1={out['detection_eval']['mean_f1']}  ({out['detection_eval']['caveat']})",
          f"- Detection, 3-vehicle fleet (seed 42): P={fe['precision']} R={fe['recall']} F1={fe['f1']} missed={fe['missed']}",
          f"- Automated tests: {out['tests']}", ""]
    (ROOT / "benchmarks" / "results.md").write_text("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
