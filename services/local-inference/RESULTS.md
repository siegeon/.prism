# Inference measurements — 2026-09-08

Intel i9-12900K, RTX 3080 Ti 12 GiB, WSL2 with 94 GiB RAM. Qwen3-30B-A3B Q4_K_M. Pinned runtime and model hashes are in README.md.

Two requests per profile: 386 input tokens, 128 forced output tokens, no prompt cache. Values are means. This is a bounded microbenchmark on a shared machine, not a global optimum or a model-quality ranking.

| Profile | Prompt tokens/s | Generation tokens/s | Request seconds |
|---|---:|---:|---:|
| hybrid-8 (initial baseline) | 96.7 | 15.4 | 12.3 |
| cpu-8 | 88.5 | 11.1 | 15.8 |
| partial-8 | 93.5 | 10.4 | 16.4 |
| hybrid-12 | 78.9 | 13.1 | 14.6 |
| hybrid-8-resident | 175.7 | 14.2 | 11.2 |
| hybrid-36 | 94.6 | 15.2 | 12.5 |

Selected `hybrid-8-resident` for lowest mean request duration and substantial GPU headroom. All expert weights stay in CPU RAM; attention is GPU-assisted. Eight generation threads, twelve batch threads, 8K context, one slot. GPU snapshot after requests showed roughly 6.4 GiB free across the whole GPU; that is not a measurement of peak use.

Raw measurements: `temp/local-inference/benchmarks.jsonl`. No electricity-cost or long-running agent-quality claim is inferred from this test.

Chat API checks passed: exact JSON incident extraction and complete SSE streaming, with thinking disabled for these checks. First content arrived in 0.49 seconds; the short streamed answer completed in 3.66 seconds. This timing uses a short prompt and is distinct from the fixed throughput benchmark.

## Context length — 2026-09-08

Same profile (`hybrid-8-resident`), same fixed prompt and 128 forced output tokens. Only `-c` changes.

| Context | Prompt tokens/s | Generation tokens/s | Request seconds | GPU used / free MiB |
|---:|---:|---:|---:|---|
| 8,192 | 175.7 | 14.2 | 11.2 | 5,499 / 6,588 |
| 32,768 | 198.2 | 14.3 | 10.8 | 6,642 / 5,445 |
| 40,960 | 214.8 | 16.2 | 9.7 | 7,397 / 4,690 |

A five-fold context increase costs about 1.9 GiB of GPU memory and no throughput. The 40,960 rows are the fastest of the three on every axis, so the 8,192 default was strictly worse. 40,960 is the length the model trains to, so this is the ceiling, not a tuning choice.

The default is now 40,960. Pass `--ctx` to measure another length.

This matters because the target workload is a conductor step. A step carries a plan, a diff and file contents, so 8,192 tokens could not hold one.
