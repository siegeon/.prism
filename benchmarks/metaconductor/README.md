# PRISM Meta-Conductor benchmark

Scores the server-side promotion policy for AutoAgent-style prompt candidates.

This is intentionally not an LLM benchmark. A calling agent may draft prompt
variants, but PRISM owns storage and promotion. This gate verifies that PRISM
promotes only candidates that beat baseline on holdout metrics while preserving
context-pack quality and operational safety signals.

Run:

```bash
python benchmarks/metaconductor/run.py
```

Results are written to `benchmarks/results/metaconductor/`.
