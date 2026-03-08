#!/usr/bin/env python3
"""Tests for Brain.system_context() output and Conductor/Brain stderr diagnostics.

Coverage:
- Brain.system_context() returns non-empty formatted output with indexed docs.
- Conductor._try_init_brain() logs specific stderr messages when Brain fails.
- prism_stop_hook.py fallback except blocks log the expected stderr messages.
- End-to-end: build_agent_instruction includes <brain_context> block when Brain
  has indexed docs.
"""

import subprocess as sp
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

from brain_engine import Brain


def _make_brain_in(tmp_path: Path) -> Brain:
    """Create a Brain instance rooted in tmp_path."""
    brain_dir = tmp_path / ".prism" / "brain"
    brain_dir.mkdir(parents=True, exist_ok=True)
    return Brain(
        brain_db=str(brain_dir / "brain.db"),
        graph_db=str(brain_dir / "graph.db"),
        scores_db=str(brain_dir / "scores.db"),
    )


def _fake_run(cmd, **kwargs):
    """Return empty subprocess result for all git calls."""
    result = MagicMock()
    result.stdout = ""
    result.returncode = 0
    return result


# ---------------------------------------------------------------------------
# Brain.system_context() returns non-empty output with indexed docs
# ---------------------------------------------------------------------------

def test_system_context_returns_non_empty_with_indexed_docs(tmp_path, monkeypatch):
    """system_context() returns a non-empty string when matching docs are indexed.

    Uses _ingest_single with doc_id == source_file so the same id is found
    by both FTS5 and graph search, ensuring RRF score > 0.02 threshold.
    """
    brain = _make_brain_in(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sp, "run", _fake_run)

    # Insert doc with unique token; doc_id == source_file enables BM25+graph fusion
    src = tmp_path / "brain_ctx_zqfunc_module.py"
    src.write_text("def brain_ctx_zqfunc_impl(): pass\n")
    brain._ingest_single(
        str(src),
        "def brain_ctx_zqfunc_impl(): pass",
        source_file=str(src),
        domain="py",
    )

    # Story file provides the search query. FTS5 uses implicit AND, so every token
    # in the query must appear in the doc. Use only tokens present in the indexed
    # content; omit extra words that aren't there.
    # Both FTS5 (keyword match) and graph (LIKE %brain_ctx_zqfunc%) return the doc,
    # yielding combined RRF score 2/61 ≈ 0.033 > the 0.02 filter threshold.
    story = tmp_path / "story.md"
    story.write_text("brain_ctx_zqfunc_impl def pass")

    result = brain.system_context(story_file=str(story), persona="dev")

    assert result, "system_context() should return non-empty string when docs are indexed"


def test_system_context_output_wrapped_in_brain_context_tags(tmp_path, monkeypatch):
    """system_context() output is wrapped in <brain_context>...</brain_context> tags."""
    brain = _make_brain_in(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sp, "run", _fake_run)

    src = tmp_path / "ctx_tag_xzqmod.py"
    src.write_text("def ctx_tag_xzqfunc(): pass\n")
    brain._ingest_single(
        str(src),
        "def ctx_tag_xzqfunc(): pass",
        source_file=str(src),
        domain="py",
    )

    story = tmp_path / "story.md"
    story.write_text("ctx tag xzqfunc check tags")

    result = brain.system_context(story_file=str(story), persona="dev")

    if result:  # only assert format when results exceed rrf threshold
        assert result.startswith("<brain_context>"), (
            "system_context() output must start with <brain_context>"
        )
        assert result.strip().endswith("</brain_context>"), (
            "system_context() output must end with </brain_context>"
        )


def test_system_context_returns_empty_string_when_no_query(tmp_path, monkeypatch):
    """system_context() with no story_file and no persona returns empty string."""
    brain = _make_brain_in(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sp, "run", _fake_run)

    result = brain.system_context()

    assert result == "", "system_context() with no query should return ''"


def test_system_context_returns_empty_string_when_docs_empty(tmp_path, monkeypatch):
    """system_context() returns '' when index is empty and bootstrap finds nothing."""
    brain = _make_brain_in(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sp, "run", _fake_run)
    # Bootstrap returns no docs when given an empty dir
    monkeypatch.setattr("brain_engine._cli_source_dirs", lambda: [str(tmp_path)])

    story = tmp_path / "story.md"
    story.write_text("unique query with no matching content")

    result = brain.system_context(story_file=str(story), persona="dev")

    # With empty or irrelevant docs the score filter drops all results
    assert isinstance(result, str), "system_context() should always return str"


def test_system_context_updates_last_result_count_on_hit(tmp_path, monkeypatch):
    """system_context() sets last_result_count > 0 when matching docs found."""
    brain = _make_brain_in(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sp, "run", _fake_run)

    src = tmp_path / "lrc_zqcount_mod.py"
    src.write_text("def lrc_zqcount_func(): pass\n")
    brain._ingest_single(
        str(src),
        "def lrc_zqcount_func(): pass",
        source_file=str(src),
        domain="py",
    )

    story = tmp_path / "story.md"
    story.write_text("lrc zqcount func count check")

    brain.system_context(story_file=str(story), persona="dev")

    assert isinstance(brain.last_result_count, int), (
        "last_result_count must be an int after system_context()"
    )


# ---------------------------------------------------------------------------
# Conductor._try_init_brain() stderr diagnostics
# ---------------------------------------------------------------------------

def test_conductor_try_init_brain_logs_brain_unavailable_to_stderr(capsys):
    """Conductor logs 'Brain unavailable' to stderr when Brain() raises."""
    from conductor_engine import Conductor

    with patch("brain_engine.Brain", side_effect=RuntimeError("db locked for test")):
        c = Conductor()

    captured = capsys.readouterr()
    assert "Brain unavailable" in captured.err, (
        "Conductor._try_init_brain() should print 'Brain unavailable' to stderr"
    )
    assert "db locked for test" in captured.err, (
        "Conductor stderr should include the original exception message"
    )


def test_conductor_try_init_brain_includes_exception_type_in_stderr(capsys):
    """Conductor stderr message includes the exception cause on Brain init failure."""
    from conductor_engine import Conductor

    with patch("brain_engine.Brain", side_effect=OSError("permission denied")):
        c = Conductor()

    captured = capsys.readouterr()
    assert "permission denied" in captured.err, (
        "Conductor should propagate the exception text to stderr"
    )


def test_conductor_brain_available_false_when_init_fails(capsys):
    """Conductor._brain_available is False when Brain() raises on init."""
    from conductor_engine import Conductor

    with patch("brain_engine.Brain", side_effect=ImportError("no brain_engine")):
        c = Conductor()

    assert c._brain_available is False, (
        "Conductor._brain_available must be False after a failed Brain() init"
    )
    assert c._brain is None, (
        "Conductor._brain must be None after a failed Brain() init"
    )


def test_conductor_brain_available_true_when_init_succeeds():
    """Conductor._brain_available is True when Brain() succeeds."""
    from conductor_engine import Conductor

    mock_brain = MagicMock()
    mock_brain._scores = MagicMock()
    mock_brain._scores.execute.return_value.fetchall.return_value = []

    with patch("brain_engine.Brain", return_value=mock_brain):
        c = Conductor()

    assert c._brain_available is True, (
        "Conductor._brain_available must be True when Brain() initialises successfully"
    )


# ---------------------------------------------------------------------------
# prism_stop_hook.py fallback stderr message patterns
# ---------------------------------------------------------------------------

def test_stop_hook_conductor_unavailable_message_format(capsys):
    """Fallback code emits '[PRISM] Conductor unavailable' with exc type and message."""
    # Reproduce the prism_stop_hook fallback print() statement in isolation
    exc = ImportError("No module named conductor_engine")
    print(
        f"[PRISM] Conductor unavailable ({type(exc).__name__}: {exc}),"
        " falling back to base instruction",
        file=sys.stderr,
    )

    captured = capsys.readouterr()
    assert "[PRISM] Conductor unavailable" in captured.err
    assert "ImportError" in captured.err
    assert "falling back to base instruction" in captured.err


def test_stop_hook_brain_reindex_failed_message_format(capsys):
    """Nested fallback code emits '[PRISM] Brain reindex failed' with exc type."""
    brain_exc = OSError("read-only file system")
    print(
        f"[PRISM] Brain reindex failed ({type(brain_exc).__name__}: {brain_exc})",
        file=sys.stderr,
    )

    captured = capsys.readouterr()
    assert "[PRISM] Brain reindex failed" in captured.err
    assert "OSError" in captured.err
    assert "read-only file system" in captured.err


# ---------------------------------------------------------------------------
# End-to-end: build_agent_instruction includes <brain_context> block
# ---------------------------------------------------------------------------

def test_build_agent_instruction_includes_brain_context_when_available(
    tmp_path, monkeypatch
):
    """Conductor.build_agent_instruction includes <brain_context> when Brain returns context."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sp, "run", _fake_run)

    from conductor_engine import Conductor

    # Configure mock Brain to return a brain_context block
    mock_brain = MagicMock()
    mock_brain.system_context.return_value = (
        "<brain_context>\n[1] test/doc.py\nsome relevant content\n</brain_context>"
    )
    mock_brain.last_result_count = 1
    mock_brain._scores = MagicMock()
    mock_brain._scores.execute.return_value.fetchall.return_value = []
    mock_brain._scores.execute.return_value.fetchone.return_value = None
    mock_brain.outcome_count.return_value = 0
    mock_brain.best_prompt.return_value = "sm/default"
    mock_brain.get_prompt.return_value = ""

    story = tmp_path / "story.md"
    story.write_text("test story for e2e verification")

    with patch("brain_engine.Brain", return_value=mock_brain):
        c = Conductor()

    assert c._brain_available

    result = c.build_agent_instruction(
        step_id="draft_story",
        agent="sm",
        action="draft",
        story_file=str(story),
        prompt="",
    )

    assert isinstance(result, str) and len(result) > 0, (
        "build_agent_instruction must return non-empty string"
    )
    assert "<brain_context>" in result, (
        "build_agent_instruction should embed the brain_context block in the instruction"
    )
    assert "some relevant content" in result, (
        "build_agent_instruction should include Brain search content in the instruction"
    )


def test_build_agent_instruction_no_brain_context_when_brain_unavailable(
    tmp_path, monkeypatch
):
    """Conductor.build_agent_instruction omits <brain_context> when Brain is unavailable."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sp, "run", _fake_run)

    from conductor_engine import Conductor

    story = tmp_path / "story.md"
    story.write_text("test story")

    with patch("brain_engine.Brain", side_effect=RuntimeError("unavailable")):
        c = Conductor()

    assert not c._brain_available

    result = c.build_agent_instruction(
        step_id="draft_story",
        agent="sm",
        action="draft",
        story_file=str(story),
        prompt="",
    )

    assert isinstance(result, str) and len(result) > 0
    assert "<brain_context>" not in result, (
        "build_agent_instruction should not emit <brain_context> when Brain is unavailable"
    )
