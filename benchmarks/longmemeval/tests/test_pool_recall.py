"""PLAT-0042 RED — Failing tests for smoke pool_recall@50 instrumentation.

Verifies AC-4 instrumentation half (T3): the smoke harness records a
per-query `gold_in_pool@50` boolean and an aggregate `pool_recall@50` in
the result JSON. Must FAIL until T3 ships.

[Source: benchmarks/longmemeval/run.py]
[Source: docs/stories/PLAT-0042-retrieval-query-decomposition.story.md]
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_HERE = Path(__file__).resolve()
_BENCH_LMM = _HERE.parent.parent
if str(_BENCH_LMM) not in sys.path:
    sys.path.insert(0, str(_BENCH_LMM))


def _import_run_module():
    try:
        import run as run_mod
        return run_mod
    except (ImportError, ModuleNotFoundError):
        return None


def test_ac4_run_module_exposes_pool_recall_helper():
    """
    AC-4 (instrumentation): The smoke harness exports a helper that
    computes `gold_in_pool@50` from a top-50 pool.
    Requirement: T3 adds this helper to benchmarks/longmemeval/run.py.
    """
    run_mod = _import_run_module()
    assert run_mod is not None, "run.py not importable from benchmarks/longmemeval"
    assert hasattr(run_mod, "compute_gold_in_pool"), (
        "run.compute_gold_in_pool not found — T3 not shipped"
    )


def test_ac4_gold_in_pool_true_when_session_present():
    """AC-4: gold_in_pool@50 is True when the gold session is in the pool."""
    run_mod = _import_run_module()
    assert run_mod is not None and hasattr(run_mod, "compute_gold_in_pool"), \
        "T3 not shipped"
    pool = [{"doc_id": f"sess-{i}"} for i in range(50)]
    pool[7]["doc_id"] = "gold-session-id"
    assert run_mod.compute_gold_in_pool(pool, "gold-session-id") is True


def test_ac4_gold_in_pool_false_when_session_absent():
    """AC-4: gold_in_pool@50 is False when the gold session is not in the pool."""
    run_mod = _import_run_module()
    assert run_mod is not None and hasattr(run_mod, "compute_gold_in_pool"), \
        "T3 not shipped"
    pool = [{"doc_id": f"sess-{i}"} for i in range(50)]
    assert run_mod.compute_gold_in_pool(pool, "gold-session-id") is False


def test_ac4_result_json_includes_pool_recall_key():
    """
    AC-4: The smoke result JSON includes a `pool_recall@50` aggregate.
    Requirement: After running smoke, the per-run result dict carries
                 the documented key.
    """
    run_mod = _import_run_module()
    assert run_mod is not None, "run.py not importable"
    assert hasattr(run_mod, "RESULT_KEYS"), (
        "run.RESULT_KEYS not exposed — T3 must declare the result schema"
    )
    assert "pool_recall@50" in run_mod.RESULT_KEYS, (
        f"`pool_recall@50` missing from result schema: {run_mod.RESULT_KEYS}"
    )


def test_ac4_run_one_queries_top_50_and_records_pool_hit():
    """
    AC-4: run_one must query a top-50 pool while still scoring R@5.
    """
    run_mod = _import_run_module()
    assert run_mod is not None, "run.py not importable"

    entry = {
        "question_id": "q-test",
        "question_type": "single-session-user",
        "question": "Where is the answer?",
        "haystack_session_ids": ["gold-session-id"],
        "haystack_sessions": [[{"role": "user", "content": "answer"}]],
        "answer_session_ids": ["gold-session-id"],
    }

    def fake_mcp_call(project, tool, arguments):
        if tool == "brain_index_doc":
            return {"result": {"content": [{"text": "{}"}]}}
        assert tool == "brain_search"
        assert arguments["limit"] == 50
        payload = [
            {"doc_id": f"miss-{i}"} for i in range(5)
        ] + [{"doc_id": "lme/q000/gold-session-id::main"}]
        return {"result": {"content": [{"text": __import__("json").dumps(payload)}]}}

    with patch.object(run_mod, "mcp_call", side_effect=fake_mcp_call):
        result = run_mod.run_one("bench-test", 0, entry)

    assert result["hit@5"] is False
    assert result["gold_in_pool@50"] is True
    assert "query_ms" in result
