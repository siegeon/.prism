"""Threshold gate for PLAT-0042 query-decomposition smoke runs.

Reads two LongMemEval smoke result JSONs (baseline and PRISM_QUERY_DECOMP=on),
and exits non-zero if any AC-4 / AC-5 threshold is breached:

  AC-4: decomp.recall@5 >= 0.96
        AND (decomp.pool_recall@50 - baseline.pool_recall@50) >= 2/n
  AC-5: decomp.median_ms / baseline.median_ms <= 1.6

Usage:
    python benchmarks/assert_thresholds.py \\
        results/longmemeval/baseline.json \\
        results/longmemeval/decomp.json --n 50
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

R5_TARGET = 0.96
LATENCY_RATIO_CAP = 1.6
POOL_DELTA_SESSIONS = 2


def check_thresholds(baseline_path: Path | str, decomp_path: Path | str, n: int = 50) -> int:
    """Return 0 if all thresholds met, 1 otherwise. Prints reasons to stderr."""
    base = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    dec = json.loads(Path(decomp_path).read_text(encoding="utf-8"))

    failures: list[str] = []
    if dec.get("recall@5", 0.0) < R5_TARGET:
        failures.append(
            f"AC-4: recall@5 {dec.get('recall@5'):.3f} < target {R5_TARGET}"
        )

    pool_delta = dec.get("pool_recall@50", 0.0) - base.get("pool_recall@50", 0.0)
    pool_floor = POOL_DELTA_SESSIONS / n
    if pool_delta < pool_floor:
        failures.append(
            f"AC-4: pool_recall@50 delta {pool_delta:+.3f} < {pool_floor:+.3f} "
            f"(need ≥{POOL_DELTA_SESSIONS} additional sessions)"
        )

    base_ms = base.get("median_ms", 0.0)
    dec_ms = dec.get("median_ms", 0.0)
    ratio = (dec_ms / base_ms) if base_ms else float("inf")
    if ratio > LATENCY_RATIO_CAP:
        failures.append(
            f"AC-5: latency ratio {ratio:.2f}× > {LATENCY_RATIO_CAP}× cap"
        )

    if failures:
        for f in failures:
            print(f"FAIL  {f}", file=sys.stderr)
        return 1
    print(
        f"PASS  R@5={dec.get('recall@5'):.3f} "
        f"Δpool={pool_delta:+.3f} latency={ratio:.2f}×",
        file=sys.stderr,
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline", type=Path)
    ap.add_argument("decomp", type=Path)
    ap.add_argument("--n", type=int, default=50)
    args = ap.parse_args()
    return check_thresholds(args.baseline, args.decomp, n=args.n)


if __name__ == "__main__":
    sys.exit(main())
