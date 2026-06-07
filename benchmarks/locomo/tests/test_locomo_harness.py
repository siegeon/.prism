"""RED — Failing tests for the LoCoMo harness port (task e043f449).

Pins AC-1: a `benchmarks/locomo/run.py` harness that mirrors MemPalace's
LoCoMo metric code (recall_any / recall_all + a temporal split) but drives
PRISM Brain's `_search` seam over OUR corpus at MemPalace-comparable
granularity, producing a recorded recall_any/recall_all + temporal number.

These pin the *integration* contract, not just an importable function:
- the harness exposes the scoring helpers (recall_any / recall_all),
- a `run_one` that actually queries the Brain search seam with a top-k pool
  and records a per-question hit AND whether the question is temporal,
- a result schema that carries recall_any / recall_all / temporal_recall,
- temporal-split aggregation that only counts temporal questions.

Must FAIL until the harness ships (no benchmarks/locomo/run.py yet).

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
_LOCOMO = _HERE.parent.parent
if str(_LOCOMO) not in sys.path:
    sys.path.insert(0, str(_LOCOMO))


def _import_run_module():
    try:
        import run as run_mod  # benchmarks/locomo/run.py
        return run_mod
    except (ImportError, ModuleNotFoundError):
        return None


def test_locomo_module_exists():
    """AC-1: benchmarks/locomo/run.py is importable."""
    run_mod = _import_run_module()
    assert run_mod is not None, "benchmarks/locomo/run.py not importable — harness not shipped"


def test_locomo_exposes_recall_helpers():
    """AC-1: recall_any / recall_all scoring helpers are exported."""
    run_mod = _import_run_module()
    assert run_mod is not None, "harness not shipped"
    assert hasattr(run_mod, "recall_any"), "recall_any not exported"
    assert hasattr(run_mod, "recall_all"), "recall_all not exported"


def test_locomo_recall_any_true_when_one_gold_retrieved():
    """recall_any: hit when ANY gold session is in the retrieved pool."""
    run_mod = _import_run_module()
    assert run_mod is not None and hasattr(run_mod, "recall_any"), "harness not shipped"
    retrieved = ["miss-1", "gold-a", "miss-2"]
    assert run_mod.recall_any(retrieved, ["gold-a", "gold-b"]) is True


def test_locomo_recall_all_requires_every_gold():
    """recall_all: only a hit when EVERY gold session is retrieved."""
    run_mod = _import_run_module()
    assert run_mod is not None and hasattr(run_mod, "recall_all"), "harness not shipped"
    assert run_mod.recall_all(["gold-a", "gold-b"], ["gold-a", "gold-b"]) is True
    assert run_mod.recall_all(["gold-a"], ["gold-a", "gold-b"]) is False


def test_locomo_result_schema_includes_temporal():
    """AC-1: result schema carries recall_any / recall_all / temporal_recall."""
    run_mod = _import_run_module()
    assert run_mod is not None, "harness not shipped"
    assert hasattr(run_mod, "RESULT_KEYS"), "RESULT_KEYS schema not declared"
    keys = run_mod.RESULT_KEYS
    for required in ("recall_any", "recall_all", "temporal_recall"):
        assert required in keys, f"`{required}` missing from result schema: {keys}"


def test_locomo_run_one_drives_search_seam_and_flags_temporal():
    """AC-1: run_one queries the Brain search seam over a top-k pool and
    records a per-question hit plus a temporal flag, driving _search (not a
    quoted MemPalace number)."""
    run_mod = _import_run_module()
    assert run_mod is not None, "harness not shipped"
    assert hasattr(run_mod, "run_one"), "run_one not exported"

    entry = {
        "question_id": "locomo-q1",
        "category": "temporal",  # LoCoMo temporal split
        "question": "When did we discuss the migration?",
        "haystack_session_ids": ["gold-session"],
        "haystack_sessions": [[{"role": "user", "content": "migration on Tuesday"}]],
        "answer_session_ids": ["gold-session"],
    }

    def fake_search(project, tool, arguments):
        if tool == "brain_index_doc":
            return {"result": {"content": [{"text": "{}"}]}}
        assert tool == "brain_search", f"harness must drive Brain search, got {tool}"
        payload = [{"doc_id": "locomo/gold-session::win_0"}]
        return {"result": {"content": [{"text": json.dumps(payload)}]}}

    with patch.object(run_mod, "mcp_call", side_effect=fake_search):
        result = run_mod.run_one("bench-locomo", 0, entry)

    assert result["recall_any"] is True, "gold session was returned; recall_any must be True"
    assert result["is_temporal"] is True, "temporal category must set is_temporal"


def test_locomo_temporal_split_isolates_temporal_questions():
    """AC-1/AC-5: temporal_recall aggregates ONLY temporal questions, so a
    temporal-boost trial can be A/B'd on the temporal split alone."""
    run_mod = _import_run_module()
    assert run_mod is not None, "harness not shipped"
    assert hasattr(run_mod, "summarize"), "summarize() not exported"
    per_q = [
        {"is_temporal": True, "recall_any": True, "recall_all": True},
        {"is_temporal": True, "recall_any": False, "recall_all": False},
        {"is_temporal": False, "recall_any": True, "recall_all": True},
    ]
    summary = run_mod.summarize(per_q)
    # temporal_recall must ignore the non-temporal hit: 1/2 = 0.5, not 2/3.
    assert summary["temporal_recall"] == 0.5, (
        f"temporal split must isolate temporal questions, got {summary['temporal_recall']}"
    )
