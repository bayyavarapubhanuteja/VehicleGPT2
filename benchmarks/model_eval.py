"""Compare local SLMs for VehicleGPT on this machine under the target budget (2 threads).

For each GGUF in models/ (each in a fresh subprocess so memory is isolated) measures: load time, process RSS after load
and peak during inference, intent-routing accuracy on benchmarks/intent_eval.json (rules only / LLM only / hybrid =
rules first, LLM for unknowns), classification latency, explanation latency, generation speed, and grounding pass rate
(explanations that introduce no unverified number). Writes benchmarks/model_eval.json and .md.
"""
from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def child(model_path: str) -> dict:
    os.environ["VEHICLEGPT_LLM"] = "llamacpp"
    os.environ["VEHICLEGPT_GGUF"] = model_path
    import psutil
    from vehiclegpt import config, db, llm, runtime
    from vehiclegpt import query_engine as qe
    proc = psutil.Process()
    rss0 = proc.memory_info().rss
    runtime.ensure_database()
    conn = db.connect()
    rss_app = proc.memory_info().rss
    t = time.perf_counter()
    ad = llm.get_adapter("llamacpp", refresh=True)
    load_ms = (time.perf_counter() - t) * 1000
    st = ad.status()
    if not st.available:
        return {"model": Path(model_path).name, "error": st.detail}
    rss_loaded = proc.memory_info().rss
    cases = json.loads((ROOT / "benchmarks" / "intent_eval.json").read_text())
    rows, lat = [], []
    peak = rss_loaded
    for q, exp in cases:
        rule = qe.classify(q)
        t = time.perf_counter()
        li = llm.classify(q, ad) or "unknown"
        lat.append((time.perf_counter() - t) * 1000)
        hybrid = rule if rule not in ("unknown",) else li
        llm_first = rule if rule in qe.STRONG_RULE_INTENTS else (li if li != "unknown" else rule)
        rows.append({"q": q, "expected": exp, "rules": rule, "llm": li, "hybrid": hybrid, "llm_first": llm_first})
        peak = max(peak, proc.memory_info().rss)
    acc = {k: round(sum(r[k] == r["expected"] for r in rows) / len(rows), 3) for k in ("rules", "llm", "hybrid", "llm_first")}
    # explanations (rephrasing verified facts) + end-to-end latency
    exp_lat, grounded, toks = [], 0, []
    e2e = []
    for q in [c[0] for c in cases[:6]]:
        t = time.perf_counter()
        a = qe.answer(conn, q, use_llm=False)
        det_ms = (time.perf_counter() - t) * 1000
        facts = "\n".join(f"- {f['label']}: {f['value']} {f['unit']}" for f in a.facts) + "\n" + a.engineer
        t = time.perf_counter()
        text, status = llm.explain(facts, "driver", ad)
        ms = (time.perf_counter() - t) * 1000
        exp_lat.append(ms)
        grounded += status == "llm"
        out = ad._llm.tokenize((text or "x").encode()) if hasattr(ad, "_llm") else []
        toks.append(len(out) / (ms / 1000) if ms else 0)
        e2e.append({"q": q, "deterministic_ms": round(det_ms, 1), "llm_explain_ms": round(ms, 1), "status": status})
        peak = max(peak, proc.memory_info().rss)
    return {"model": Path(model_path).name, "file_mb": round(Path(model_path).stat().st_size / 1e6),
            "threads": config.LLM_THREADS, "ctx": config.LLM_CTX, "load_ms": round(load_ms),
            "rss_python_app_mb": round(rss_app / 1e6), "rss_after_load_mb": round(rss_loaded / 1e6),
            "rss_peak_mb": round(peak / 1e6), "rss_baseline_mb": round(rss0 / 1e6),
            "intent_accuracy": acc, "classify_ms_median": round(statistics.median(lat)), "classify_ms_p95": round(sorted(lat)[int(.95 * (len(lat) - 1))]),
            "explain_ms_median": round(statistics.median(exp_lat)), "explain_tokens_per_s": round(statistics.median(toks), 1),
            "grounding_pass_rate": round(grounded / len(exp_lat), 2), "errors": [r for r in rows if r["llm_first"] != r["expected"]],
            "end_to_end": e2e}


def main():
    models = sorted((ROOT / "models").glob("*.gguf"))
    results = []
    for m in models:
        print(f"evaluating {m.name} …", flush=True)
        p = subprocess.run([sys.executable, __file__, "--child", str(m)], capture_output=True, text=True, cwd=ROOT,
                           env={**os.environ, "VEHICLEGPT_NET_PROBE": "0"})
        try:
            results.append(json.loads(p.stdout.strip().splitlines()[-1]))
        except Exception:  # noqa: BLE001
            results.append({"model": m.name, "error": p.stderr[-800:]})
    out = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "platform": platform.platform(), "cpu": platform.processor(),
           "note": "x86_64 host limited to 2 inference threads; ARM A5x will be slower per thread — see docs/EDGE_DEPLOYMENT.md",
           "results": results}
    (ROOT / "benchmarks" / "model_eval.json").write_text(json.dumps(out, indent=2))
    md = ["# Local SLM comparison", "", f"{out['timestamp']} · {out['platform']} · {out['note']}", "",
          "| model | file MB | load ms | RSS after load MB | peak RSS MB | intent acc rules / LLM / rules-first / LLM-first | classify ms (median/p95) | explain ms | tok/s | grounded |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if "error" in r:
            md.append(f"| {r['model']} | error: {r['error'][:80]} |"); continue
        a = r["intent_accuracy"]
        md.append(f"| {r['model']} | {r['file_mb']} | {r['load_ms']} | {r['rss_after_load_mb']} | {r['rss_peak_mb']} | "
                  f"{a['rules']} / {a['llm']} / {a['hybrid']} / {a['llm_first']} | {r['classify_ms_median']} / {r['classify_ms_p95']} | "
                  f"{r['explain_ms_median']} | {r['explain_tokens_per_s']} | {r['grounding_pass_rate']} |")
    (ROOT / "benchmarks" / "model_eval.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        print(json.dumps(child(sys.argv[2])))
    else:
        main()
