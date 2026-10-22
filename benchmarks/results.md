# Benchmark results (2026-10-20T13:43:50)

Conditions: `{"python": "3.11.9", "platform": "macOS-26.6.2-x86_64-i386-64bit", "machine": "x86_64", "processor": "i386", "cpu_count": 16, "reps_per_query": 20}`

- Cold start: dataset generation 5749 ms, detection 1138 ms
- DB 7-day telemetry query: median 35.53 ms
- Query latency (deterministic): median-of-medians 45.9 ms, worst median 236.9 ms; intent accuracy 9/9
- Memory: Python heap peak 4.7 MB, process RSS 113.8 MB
- LLM: deterministic-fallback — No local model available — GGUF model not found at /Users/apple/Desktop/VehicleGPT2/models/model.gguf; Ollama not reachable at http://127.0.0.1:11434 (URLError)
- Detection (6 seeds): P=1.0 R=1.0 F1=1.0  (Rules were designed with knowledge of the injected scenarios (seed 42); other seeds change noise only. Scores indicate rule correctness on synthetic data, not real-world accuracy.)
- Detection, 3-vehicle fleet (seed 42): P=0.95 R=0.947 F1=0.949 missed=['GT2-HWY-1']
- Automated tests: 140 passed in 27.53s
