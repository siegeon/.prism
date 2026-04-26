"""PLAT-0042 RED — Failing tests for the query_decomposer module.

These tests verify AC-1 of PLAT-0042 and must FAIL until T1 ships.

[Source: docs/stories/PLAT-0042-retrieval-query-decomposition.story.md]
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _import_decompose():
    """Import the function under test, returning None if not yet shipped.

    Convert ImportError into a clean None so the assertion in each test
    fails with a readable message rather than crashing the collector.
    """
    try:
        from app.engines.query_decomposer import decompose_query
        return decompose_query
    except (ImportError, ModuleNotFoundError):
        return None


def test_ac1_module_exists_and_is_callable():
    """
    AC-1: Decomposer is shipped as a callable.
    Requirement: `decompose_query(q: str, max_subs: int = 4) -> list[str]`
                 lives at app.engines.query_decomposer.
    Expected: import succeeds and the symbol is callable.
    """
    fn = _import_decompose()
    assert fn is not None, (
        "app.engines.query_decomposer.decompose_query is not importable yet — T1 not shipped"
    )
    assert callable(fn), "decompose_query must be callable"


def test_ac1_trivial_query_returns_only_raw():
    """
    AC-1: Trivial inputs (≤6 tokens, no connective) return [q] only.
    """
    fn = _import_decompose()
    assert fn is not None, "T1 not shipped"
    out = fn("auth bug repro")
    assert out == ["auth bug repro"], f"trivial query should return [q], got {out!r}"


def test_ac1_compound_query_returns_multiple_subqueries():
    """
    AC-1: A compound question with " and " produces ≥2 sub-queries
          AND the raw query is preserved in the output.
    """
    fn = _import_decompose()
    assert fn is not None, "T1 not shipped"
    q = "how did Bob fix the auth bug and what was the migration plan"
    out = fn(q)
    assert len(out) >= 2, f"compound query should yield ≥2 sub-queries, got {out!r}"
    assert q in out, f"raw query must be preserved in output, got {out!r}"


def test_ac1_long_query_triggers_decomposition_above_12_tokens():
    """
    AC-1: Queries exceeding 12 tokens trigger decomposition even
          without explicit connectives.
    """
    fn = _import_decompose()
    assert fn is not None, "T1 not shipped"
    q = "find the function that handles authentication failures during multi tenant database migration scripts"
    assert len(q.split()) > 12
    out = fn(q)
    assert len(out) >= 2, (
        f"queries >12 tokens should decompose, got {len(out)} sub-queries: {out!r}"
    )


def test_ac1_temporal_memory_question_adds_name_fallback():
    """
    AC-1: Temporal personal-memory questions keep a name-only fallback.
    """
    fn = _import_decompose()
    assert fn is not None, "T1 not shipped"
    q = "What did I do with Rachel on the Wednesday two months ago?"
    out = fn(q)
    assert q in out, f"raw query must be preserved in output, got {out!r}"
    assert "Rachel" in out, f"temporal name fallback missing from {out!r}"


def test_ac1_empty_string_returns_singleton():
    """AC-1 edge case: empty string returns [''] (raw fallback)."""
    fn = _import_decompose()
    assert fn is not None, "T1 not shipped"
    out = fn("")
    assert out == [""], f"empty input should return [''], got {out!r}"


def test_ac1_dedupe_case_insensitive():
    """AC-1: Sub-queries are deduped case-insensitively."""
    fn = _import_decompose()
    assert fn is not None, "T1 not shipped"
    out = fn("auth and Auth and AUTH")
    lowered = [s.lower() for s in out]
    assert len(lowered) == len(set(lowered)), f"duplicates not removed: {out!r}"


def test_ac1_max_subs_cap_respected():
    """AC-1: Output is capped at max_subs."""
    fn = _import_decompose()
    assert fn is not None, "T1 not shipped"
    q = "find a and find b and find c and find d and find e and find f"
    out = fn(q, max_subs=3)
    assert len(out) <= 3, f"max_subs=3 must cap output, got {len(out)}: {out!r}"


def test_ac1_no_llm_dependency():
    """
    AC-1 / R-3: Module must not import any LLM client library.
    Requirement: pure-Python rules-based v1, no openai/anthropic imports.
    """
    fn = _import_decompose()
    assert fn is not None, "T1 not shipped"
    import app.engines.query_decomposer as qd
    src = Path(qd.__file__).read_text(encoding="utf-8")
    forbidden = ["import openai", "import anthropic", "from openai", "from anthropic"]
    hits = [s for s in forbidden if s in src]
    assert not hits, f"LLM client imports forbidden in v1: found {hits}"
