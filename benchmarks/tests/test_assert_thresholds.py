"""PLAT-0042 RED — Failing tests for the threshold-assertion gate.

Closes the automation gap flagged by the requirements-tracer: AC-4
(R@5 ≥ 0.96, ≥+2 pool sessions) and AC-5 (median latency ≤ 1.6×
baseline) must be machine-checkable, not just human-read off
EXPERIMENTS.md.

These tests verify a small `benchmarks/assert_thresholds.py` script
that loads the latest LongMemEval smoke result JSON plus a baseline
record, and exits non-zero when any AC-4/AC-5 threshold is breached.
Must FAIL until the script ships in T4.

[Source: docs/stories/PLAT-0042-retrieval-query-decomposition.story.md]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_BENCH = _HERE.parent.parent
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))


def _import_thresholds():
    try:
        import assert_thresholds as mod
        return mod
    except (ImportError, ModuleNotFoundError):
        return None


def _write_results(tmp_path, baseline, decomp):
    bp = tmp_path / "baseline.json"
    dp = tmp_path / "decomp.json"
    bp.write_text(json.dumps(baseline), encoding="utf-8")
    dp.write_text(json.dumps(decomp), encoding="utf-8")
    return bp, dp


def test_ac4_module_exists():
    """
    AC-4/AC-5 gate: assert_thresholds.py is shipped with check_thresholds().
    """
    mod = _import_thresholds()
    assert mod is not None, "benchmarks/assert_thresholds.py not importable — T4 not shipped"
    assert hasattr(mod, "check_thresholds"), "check_thresholds() not exported"


def test_ac4_passes_when_recall_lift_meets_target(tmp_path):
    """
    AC-4: Pass when R@5 ≥ 0.96 AND ≥+2 pool sessions added.
    """
    mod = _import_thresholds()
    assert mod is not None and hasattr(mod, "check_thresholds"), "T4 not shipped"
    baseline = {"recall@5": 0.940, "pool_recall@50": 0.94, "median_ms": 100.0}
    decomp = {"recall@5": 0.962, "pool_recall@50": 1.00, "median_ms": 140.0}
    bp, dp = _write_results(tmp_path, baseline, decomp)
    assert mod.check_thresholds(bp, dp, n=50) == 0, "should pass when all thresholds met"


def test_ac4_fails_when_recall_below_target(tmp_path):
    """
    AC-4: Fail when R@5 < 0.96 even if pool delta is positive.
    """
    mod = _import_thresholds()
    assert mod is not None and hasattr(mod, "check_thresholds"), "T4 not shipped"
    baseline = {"recall@5": 0.940, "pool_recall@50": 0.94, "median_ms": 100.0}
    decomp = {"recall@5": 0.948, "pool_recall@50": 1.00, "median_ms": 120.0}
    bp, dp = _write_results(tmp_path, baseline, decomp)
    rc = mod.check_thresholds(bp, dp, n=50)
    assert rc != 0, "should fail when R@5 < 0.96"


def test_ac4_fails_when_pool_delta_below_two_sessions(tmp_path):
    """
    AC-4: Fail when fewer than 2 additional gold sessions enter the pool.
    With N=50 the delta floor is +2/50 = 0.04 in pool_recall@50.
    """
    mod = _import_thresholds()
    assert mod is not None and hasattr(mod, "check_thresholds"), "T4 not shipped"
    baseline = {"recall@5": 0.940, "pool_recall@50": 0.94, "median_ms": 100.0}
    # +1 session → delta 0.02, below the 0.04 floor.
    decomp = {"recall@5": 0.962, "pool_recall@50": 0.96, "median_ms": 120.0}
    bp, dp = _write_results(tmp_path, baseline, decomp)
    rc = mod.check_thresholds(bp, dp, n=50)
    assert rc != 0, "should fail when pool delta < 2 sessions"


def test_ac5_fails_when_latency_ratio_above_cap(tmp_path):
    """
    AC-5: Fail when median latency ratio exceeds 1.6×.
    """
    mod = _import_thresholds()
    assert mod is not None and hasattr(mod, "check_thresholds"), "T4 not shipped"
    baseline = {"recall@5": 0.940, "pool_recall@50": 0.94, "median_ms": 100.0}
    decomp = {"recall@5": 0.965, "pool_recall@50": 1.00, "median_ms": 170.0}
    bp, dp = _write_results(tmp_path, baseline, decomp)
    rc = mod.check_thresholds(bp, dp, n=50)
    assert rc != 0, "should fail when median latency > 1.6× baseline"
