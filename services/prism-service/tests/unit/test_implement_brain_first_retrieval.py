"""Child 3a3f90da (epic 95474ec7 AC-1): a drive retrieves BRAIN-FIRST.

The implement workflow's Locate step must query the Brain (brain_search /
memory_recall) BEFORE any grep, and carry the result forward as the
brain-first context_summary every later step reads. The PRISM SPA and
workflow scripts have no JS test runner, so this pins the ACTUAL source of
.claude/workflows/implement.js (same convention as
test_implement_workflow_claim_first.py).
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
IMPLEMENT_JS = REPO / ".claude/workflows/implement.js"


def _src() -> str:
    return IMPLEMENT_JS.read_text(encoding="utf-8")


def test_locate_step_is_declared_brain_first():
    src = _src()
    assert "title: 'Locate'" in src
    assert "brain-first context" in src, "Locate's meta detail must promise brain-first context"


def test_locate_prompt_queries_the_brain_first():
    src = _src()
    line = next(l for l in src.splitlines() if "FIRST query the Brain" in l)
    assert "brain_search" in line and "memory_recall" in line, (
        "the Locate prompt's FIRST instruction must name brain_search + memory_recall"
    )


def test_locate_output_carries_a_brain_first_context_summary():
    src = _src()
    assert "context_summary" in src
    assert "brain-first summary of the relevant subsystem" in src, (
        "the Locate schema's context_summary must be described as brain-first so "
        "every later step inherits Brain-derived context, not grep output"
    )
