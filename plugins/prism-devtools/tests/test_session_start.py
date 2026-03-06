"""Tests for Phase 4: session-start hook — auto-memory redirect + Brain bootstrap.

AC-1: _ensure_memory_dir() creates .prism/brain/memory/ and seeds MEMORY.md
AC-2: MEMORY.md not overwritten if it already exists
AC-3: session-start.py outputs valid JSON systemMessage
AC-4: systemMessage includes Brain doc count and auto-memory path
AC-5: incremental_reindex is called during startup
"""
import json
import sys
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import importlib.util
import pytest

# Ensure hooks directory is on path
HOOKS_DIR = Path(__file__).parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

# session-start.py uses a hyphen so we load it via importlib
_spec = importlib.util.spec_from_file_location("session_start", HOOKS_DIR / "session-start.py")
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)


# ---------------------------------------------------------------------------
# AC-1: _ensure_memory_dir creates directory and seeds MEMORY.md
# ---------------------------------------------------------------------------

class TestAC1_EnsureMemoryDir:
    def test_ac1_creates_directory(self, tmp_path):
        """AC-1: _ensure_memory_dir creates .prism/brain/memory/ if absent."""
        result = ss._ensure_memory_dir(tmp_path)
        assert result.exists(), ".prism/brain/memory/ directory must be created"
        assert result.is_dir()

    def test_ac1_seeds_memory_md(self, tmp_path):
        """AC-1: _ensure_memory_dir creates MEMORY.md if absent."""
        ss._ensure_memory_dir(tmp_path)
        mem_file = tmp_path / ".prism" / "brain" / "memory" / "MEMORY.md"
        assert mem_file.exists(), "MEMORY.md must be seeded"

    def test_ac1_memory_md_contains_brain_instructions(self, tmp_path):
        """AC-1: seeded MEMORY.md contains Brain query instructions."""
        ss._ensure_memory_dir(tmp_path)
        content = (tmp_path / ".prism" / "brain" / "memory" / "MEMORY.md").read_text()
        assert "Brain" in content
        assert "mulch record" in content


# ---------------------------------------------------------------------------
# AC-2: MEMORY.md not overwritten if it already exists
# ---------------------------------------------------------------------------

class TestAC2_NoOverwrite:
    def test_ac2_existing_memory_not_overwritten(self, tmp_path):
        """AC-2: _ensure_memory_dir preserves existing MEMORY.md content."""
        mem_dir = tmp_path / ".prism" / "brain" / "memory"
        mem_dir.mkdir(parents=True)
        existing = "# My custom notes\n- important fact\n"
        (mem_dir / "MEMORY.md").write_text(existing)

        ss._ensure_memory_dir(tmp_path)

        content = (mem_dir / "MEMORY.md").read_text()
        assert content == existing, "Existing MEMORY.md must not be overwritten"


# ---------------------------------------------------------------------------
# AC-3: session-start.py outputs valid JSON systemMessage
# ---------------------------------------------------------------------------

class TestAC3_OutputFormat:
    def test_ac3_outputs_valid_json(self, tmp_path, monkeypatch, capsys):
        """AC-3: main() outputs a valid JSON object."""
        monkeypatch.chdir(tmp_path)

        with patch.object(ss, "_find_project_root", return_value=tmp_path), \
             patch.object(ss, "_run_reindex", return_value=0), \
             patch.object(ss, "_brain_doc_count", return_value=0):
            ss.main()

        captured = capsys.readouterr()
        data = json.loads(captured.out.strip())
        assert isinstance(data, dict), "Output must be a JSON object"

    def test_ac3_has_system_message_key(self, tmp_path, monkeypatch, capsys):
        """AC-3: JSON output has 'systemMessage' key."""
        monkeypatch.chdir(tmp_path)

        with patch.object(ss, "_find_project_root", return_value=tmp_path), \
             patch.object(ss, "_run_reindex", return_value=0), \
             patch.object(ss, "_brain_doc_count", return_value=0):
            ss.main()

        captured = capsys.readouterr()
        data = json.loads(captured.out.strip())
        assert "systemMessage" in data, "JSON must contain 'systemMessage' key"


# ---------------------------------------------------------------------------
# AC-4: systemMessage includes Brain doc count and auto-memory path
# ---------------------------------------------------------------------------

class TestAC4_SystemMessage:
    def _get_message(self, tmp_path, monkeypatch, capsys, doc_count=42, reindexed=3):
        monkeypatch.chdir(tmp_path)
        with patch.object(ss, "_find_project_root", return_value=tmp_path), \
             patch.object(ss, "_run_reindex", return_value=reindexed), \
             patch.object(ss, "_brain_doc_count", return_value=doc_count):
            ss.main()
        captured = capsys.readouterr()
        return json.loads(captured.out.strip())["systemMessage"]

    def test_ac4_includes_doc_count(self, tmp_path, monkeypatch, capsys):
        """AC-4: systemMessage includes the number of indexed docs."""
        msg = self._get_message(tmp_path, monkeypatch, capsys, doc_count=42)
        assert "42" in msg, "systemMessage must include doc count"

    def test_ac4_includes_memory_path(self, tmp_path, monkeypatch, capsys):
        """AC-4: systemMessage includes the auto-memory path."""
        msg = self._get_message(tmp_path, monkeypatch, capsys)
        assert "memory" in msg.lower(), "systemMessage must reference memory path"

    def test_ac4_includes_reindex_count_when_nonzero(self, tmp_path, monkeypatch, capsys):
        """AC-4: systemMessage includes reindexed file count when > 0."""
        msg = self._get_message(tmp_path, monkeypatch, capsys, reindexed=3)
        assert "3" in msg, "systemMessage must include reindex count when nonzero"

    def test_ac4_no_reindex_note_when_zero(self, tmp_path, monkeypatch, capsys):
        """AC-4: systemMessage omits reindex note when count is 0."""
        msg = self._get_message(tmp_path, monkeypatch, capsys, reindexed=0)
        assert "reindexed" not in msg, "No reindex note when 0 files reindexed"


# ---------------------------------------------------------------------------
# AC-5: incremental_reindex is called during startup
# ---------------------------------------------------------------------------

class TestAC5_ReindexCalled:
    def test_ac5_reindex_called_on_main(self, tmp_path, monkeypatch, capsys):
        """AC-5: main() calls _run_reindex exactly once."""
        monkeypatch.chdir(tmp_path)
        called = []

        def fake_reindex(root):
            called.append(root)
            return 0

        with patch.object(ss, "_find_project_root", return_value=tmp_path), \
             patch.object(ss, "_run_reindex", side_effect=fake_reindex), \
             patch.object(ss, "_brain_doc_count", return_value=0):
            ss.main()

        assert len(called) == 1, "_run_reindex must be called exactly once"
