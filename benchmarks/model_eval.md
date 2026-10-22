# Local SLM comparison

2026-10-20T14:28:31 · macOS-26.6.2-x86_64-i386-64bit · x86_64 host limited to 2 inference threads; ARM A5x will be slower per thread — see docs/EDGE_DEPLOYMENT.md

| model | file MB | load ms | RSS after load MB | peak RSS MB | intent acc rules / LLM / rules-first / LLM-first | classify ms (median/p95) | explain ms | tok/s | grounded |
|---|---|---|---|---|---|---|---|---|---|
| Llama-3.2-1B-Instruct-Q4_K_M.gguf | 808 | 5892 | 1168 | 1179 | 0.675 / 0.4 / 0.725 / 0.75 | 361 / 683 | 7418 | 6.1 | 0.5 |
| qwen2.5-0.5b-instruct-q4_k_m.gguf | 491 | 3416 | 759 | 769 | 0.675 / 0.425 / 0.75 / 0.6 | 234 / 394 | 4143 | 9.7 | 0.5 |
| qwen2.5-1.5b-instruct-q4_k_m.gguf | 1117 | 5990 | 1449 | 1458 | 0.675 / 0.95 / 0.875 / 0.975 | 469 / 668 | 10118 | 4.4 | 0.5 |
