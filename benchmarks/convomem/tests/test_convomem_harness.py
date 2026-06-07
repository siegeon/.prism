"""RED — Failing tests for the ConvoMem harness port (task e043f449).

Pins AC-2: a `benchmarks/convomem/run.py` harness mirroring MemPalace's
ConvoMem metric code, driving PRISM Brain's `_search` seam and producing a
recorded recall number across the 6 conversational categories.

Pins the integration contract:
- the 6 ConvoMem categories are declared,
- a `run_one` that drives the Brain search seam and records a hit,
- per-category recall aggregation in the summary.

Must FAIL until benchmarks/convomem/run.py ships.

[Source: benchmarks/longmemeval/run.py — established harness pattern]
[Source: services/prism-service/prism_service/engines/brain_engine.py::Brain.search :2511]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_HERE = Path(__file__).resolve()
_CONVOMEM = _HERE.parent.parent
if str(_CONVOMEM) not in sys.path:
    sys.path.insert(0, str(_CONVOMEM))


def _import_run_module():
    try:
        import run as run_mod  # benchmarks/convomem/run.py
        return run_mod
    except (ImportError, ModuleNotFoundError):
        return None


def test_convomem_module_exists():
    """AC-2: benchmarks/convomem/run.py is importable."""
    run_mod = _import_run_module()
    assert run_mod is not None, "benchmarks/convomem/run.py not importable — harness not shipped"


def test_convomem_declares_six_categories():
    """AC-2: the 6 ConvoMem conversational categories are declared."""
    run_mod = _import_run_module()
    assert run_mod is not None, "harness not shipped"
    assert hasattr(run_mod, "CATEGORIES"), "CATEGORIES not declared"
    assert len(run_mod.CATEGORIES) == 6, (
        f"ConvoMem has 6 conversational categories, got {len(run_mod.CATEGORIES)}: "
        f"{run_mod.CATEGORIES}"
    )


def test_convomem_result_schema_has_recall_and_by_category():
    """AC-2: result schema records aggregate recall and per-category recall."""
    run_mod = _import_run_module()
    assert run_mod is not None, "harness not shipped"
    assert hasattr(run_mod, "RESULT_KEYS"), "RESULT_KEYS schema not declared"
    keys = run_mod.RESULT_KEYS
    assert "recall" in keys, f"`recall` missing from schema: {keys}"
    assert "by_category" in keys, f"`by_category` missing from schema: {keys}"


def test_convomem_run_one_drives_search_seam():
    """AC-2: run_one drives the Brain search seam (not a quoted number)."""
    run_mod = _import_run_module()
    assert run_mod is not None, "harness not shipped"
    assert hasattr(run_mod, "run_one"), "run_one not exported"

    entry = {
        "question_id": "convo-q1",
        "category": "single-session-preference",
        "question": "What coffee do I like?",
        "haystack_session_ids": ["gold-session"],
        "haystack_sessions": [[{"role": "user", "content": "I love oat-milk latte"}]],
        "answer_session_ids": ["gold-session"],
    }

    def fake_search(project, tool, arguments):
        if tool == "brain_index_doc":
            return {"result": {"content": [{"text": "{}"}]}}
        assert tool == "brain_search", f"harness must drive Brain search, got {tool}"
        payload = [{"doc_id": "convomem/gold-session::__file__"}]
        return {"result": {"content": [{"text": json.dumps(payload)}]}}

    with patch.object(run_mod, "mcp_call", side_effect=fake_search):
        result = run_mod.run_one("bench-convomem", 0, entry)

    assert result["hit"] is True, "gold session returned; hit must be True"
    assert result["category"] == "single-session-preference"


def test_convomem_summary_aggregates_per_category_recall():
    """AC-2: summarize() reports recall per conversational category."""
    run_mod = _import_run_module()
    assert run_mod is not None, "harness not shipped"
    assert hasattr(run_mod, "summarize"), "summarize() not exported"
    per_q = [
        {"category": "single-session-preference", "hit": True},
        {"category": "single-session-preference", "hit": False},
        {"category": "multi-session", "hit": True},
    ]
    summary = run_mod.summarize(per_q)
    assert summary["by_category"]["single-session-preference"]["recall"] == 0.5
    assert summary["by_category"]["multi-session"]["recall"] == 1.0
