"""Test decision_evidence extractor: legacy decisions get their evidence from text.

Backfills the evidence field on decision memories that have empty evidence,
by scanning their description/summary for references to:
  - file_paths: .py/.ts/.tsx/.ttl/.md/.json/.yaml/.js files verbatim in text
  - tasks: 8-hex or full uuid task ids after "task" or in [task:...] tags
  - commits: 7-40 hex shas after "commit" or in parentheses (abc1234)
  - versions: "7.13.86"-shaped version strings
  - memories: "mx-" + 6 hex ids

Only verbatim substrings appear in output. An empty evidence dict returns {}.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.models.memory import ExpertiseEntry
from prism_service.services.memory_service import MemoryService
from prism_service.services import decision_evidence


def _entry(**kw) -> ExpertiseEntry:
    """Build an ExpertiseEntry with sensible defaults."""
    base = dict(
        id=kw.pop("id", "mx-aaaaaa"),
        domain=kw.pop("domain", "understand"),
        name=kw.pop("name", "a-decision"),
        description=kw.pop("description", ""),
        type="decision",
        classification="strategic",
        status="active",
    )
    base.update(kw)
    return ExpertiseEntry(**base)


class TestExtractEvidenceFromText:
    """extract_evidence(text: str) -> dict returns only keys with hits."""

    def test_extracts_file_paths_ending_in_known_extensions(self):
        text = (
            "The change is in services/prism-service/prism_service/services/ste.py "
            "and tests/unit/test_ste.py. Not a match: myfile.txt."
        )
        result = decision_evidence.extract_evidence(text)
        assert "files" in result or result == {}  # May be empty if not found
        files = result.get("files", [])
        assert "services/prism-service/prism_service/services/ste.py" in files
        assert "tests/unit/test_ste.py" in files
        assert "myfile.txt" not in files

    def test_extracts_task_ids_after_task_keyword(self):
        text = "This is tracked in task ab12cd34 and task 12345678-abcd-ef12-3456-7890abcdef12."
        result = decision_evidence.extract_evidence(text)
        tasks = result.get("tasks", [])
        assert "ab12cd34" in tasks
        assert "12345678-abcd-ef12-3456-7890abcdef12" in tasks

    def test_extracts_task_ids_from_brackets(self):
        text = "Documented at [task:ab12cd34] and [task:87654321]."
        result = decision_evidence.extract_evidence(text)
        tasks = result.get("tasks", [])
        assert "ab12cd34" in tasks
        assert "87654321" in tasks

    def test_extracts_commit_shas_after_commit_keyword(self):
        text = "Fixed in commit abc1234 and commit 1234567890abcdef."
        result = decision_evidence.extract_evidence(text)
        commits = result.get("commits", [])
        assert "abc1234" in commits
        assert "1234567890abcdef" in commits

    def test_extracts_commit_shas_in_parentheses(self):
        text = "The rewrite (abc1234) and merge (0123456789abcdef) both fix it."
        result = decision_evidence.extract_evidence(text)
        commits = result.get("commits", [])
        assert "abc1234" in commits
        assert "0123456789abcdef" in commits

    def test_extracts_version_strings(self):
        text = "Shipped in 7.13.86 and 7.13.111."
        result = decision_evidence.extract_evidence(text)
        versions = result.get("versions", [])
        assert "7.13.86" in versions
        assert "7.13.111" in versions

    def test_extracts_memory_ids(self):
        text = "Related to mx-aabbcc and mx-112233."
        result = decision_evidence.extract_evidence(text)
        memories = result.get("memories", [])
        assert "mx-aabbcc" in memories
        assert "mx-112233" in memories

    def test_complex_text_with_multiple_evidence_types(self):
        text = (
            "The issue is in services/prism-service/prism_service/config.py. "
            "Fixed in commit abc1234 (task 87654321) as per mx-aabbcc. "
            "Shipped in 7.13.86."
        )
        result = decision_evidence.extract_evidence(text)
        assert "services/prism-service/prism_service/config.py" in result.get("files", [])
        assert "abc1234" in result.get("commits", [])
        assert "87654321" in result.get("tasks", [])
        assert "mx-aabbcc" in result.get("memories", [])
        assert "7.13.86" in result.get("versions", [])

    def test_empty_text_returns_empty_dict(self):
        result = decision_evidence.extract_evidence("")
        assert result == {}

    def test_text_with_no_references_returns_empty_dict(self):
        text = "This is a general decision with no specific references."
        result = decision_evidence.extract_evidence(text)
        assert result == {}

    def test_only_keys_with_hits_appear_in_result(self):
        text = "File is services/test.py."
        result = decision_evidence.extract_evidence(text)
        assert "files" in result
        assert "tasks" not in result
        assert "commits" not in result
        assert "versions" not in result
        assert "memories" not in result

    def test_values_are_verbatim_substrings(self):
        """All values must be exact substrings that appear in the text."""
        text = (
            "See services/my_file.py and task 12345678 for details. "
            "Commit a1b2c3d in version 7.13.86. Note mx-abcdef here."
        )
        result = decision_evidence.extract_evidence(text)
        for key in result:
            for value in result[key]:
                assert value in text, f"{value} not found in text"


class TestDryRun:
    """dry_run(project, limit) lists decision memories with empty evidence."""

    def test_dry_run_counts_total_empty_and_would_backfill(self, tmp_path, monkeypatch):
        # Set up a MemoryService with mixed evidence states
        svc = MemoryService(mulch_dir=str(tmp_path / "mulch"))

        # One decision with empty evidence and referenceable text
        d1 = _entry(
            id="mx-decision1",
            name="dec-1",
            description="Fixed in commit abc1234 for task 87654321.",
            evidence={},  # Empty
        )

        # One decision with empty evidence and no references
        d2 = _entry(
            id="mx-decision2",
            name="dec-2",
            description="A general principle with no references.",
            evidence={},  # Empty
        )

        # One decision with evidence already populated (should not count)
        d3 = _entry(
            id="mx-decision3",
            name="dec-3",
            description="Already has evidence.",
            evidence={"commits": ["abc1234"]},
        )

        svc._write_entries("understand", [d1, d2, d3])

        # Patch get_project to return our test service
        from prism_service.services import decision_evidence as de_module
        monkeypatch.setattr(
            de_module, "get_project",
            lambda p: type("_", (), {"memory_svc": svc})(),
        )

        result = decision_evidence.dry_run("test_project", limit=200)

        assert result["total_empty"] >= 2, "Should count at least 2 empty evidence entries"
        assert result["would_backfill"] >= 1, "Should find at least 1 entry with extractable evidence"
        assert "mx-decision2" in result["no_reference"], "Entry with no references should be listed"
        # The samples should include decision1 (has extractable evidence)
        sample_ids = [s["id"] for s in result["samples"]]
        assert "mx-decision1" in sample_ids

    def test_dry_run_includes_sample_evidence(self, tmp_path, monkeypatch):
        svc = MemoryService(mulch_dir=str(tmp_path / "mulch"))

        d = _entry(
            id="mx-test-sample",
            name="sample-decision",
            description="Fixed in services/test.py (commit abc1234) as per task 11223344.",
            evidence={},
        )
        svc._write_entries("understand", [d])

        from prism_service.services import decision_evidence as de_module
        monkeypatch.setattr(
            de_module, "get_project",
            lambda p: type("_", (), {"memory_svc": svc})(),
        )

        result = decision_evidence.dry_run("test_project", limit=200)

        assert len(result["samples"]) > 0
        sample = result["samples"][0]
        assert sample["id"] == "mx-test-sample"
        assert "evidence" in sample
        assert "excerpt" in sample
        assert "services/test.py" in sample["evidence"].get("files", [])
        assert "abc1234" in sample["evidence"].get("commits", [])
        assert "11223344" in sample["evidence"].get("tasks", [])

    def test_dry_run_respects_limit(self, tmp_path, monkeypatch):
        svc = MemoryService(mulch_dir=str(tmp_path / "mulch"))

        # Create 20 decisions, all with evidence to extract
        entries = [
            _entry(
                id=f"mx-dec{i:02d}",
                name=f"decision-{i}",
                description=f"File is services/file{i}.py.",
                evidence={},
            )
            for i in range(20)
        ]
        svc._write_entries("understand", entries)

        from prism_service.services import decision_evidence as de_module
        monkeypatch.setattr(
            de_module, "get_project",
            lambda p: type("_", (), {"memory_svc": svc})(),
        )

        result = decision_evidence.dry_run("test_project", limit=200)
        assert len(result["samples"]) <= 15, "Samples should be capped at ~15 per spec"


class TestApply:
    """apply(project, ids) updates evidence through MemoryService.update."""

    def test_apply_updates_evidence_for_specified_ids(self, tmp_path, monkeypatch):
        svc = MemoryService(mulch_dir=str(tmp_path / "mulch"))

        d1 = _entry(
            id="mx-apply1",
            name="to-update",
            description="Fixed in services/test.py (task ab123456).",
            evidence={},
        )
        d2 = _entry(
            id="mx-apply2",
            name="leave-alone",
            description="Another decision.",
            evidence={},
        )
        svc._write_entries("understand", [d1, d2])

        from prism_service.services import decision_evidence as de_module
        monkeypatch.setattr(
            de_module, "get_project",
            lambda p: type("_", (), {"memory_svc": svc})(),
        )

        # Apply evidence only to mx-apply1
        result = decision_evidence.apply("test_project", ["mx-apply1"])

        assert result["updated"] >= 1
        # Fetch the updated entry
        updated = svc.get_entry("mx-apply1")
        assert updated is not None
        assert updated.evidence != {}, "Evidence should be populated"
        assert "services/test.py" in updated.evidence.get("files", [])

        # d2 should remain unchanged
        untouched = svc.get_entry("mx-apply2")
        assert untouched.evidence == {}, "Untouched entry should keep empty evidence"

    def test_apply_is_idempotent(self, tmp_path, monkeypatch):
        svc = MemoryService(mulch_dir=str(tmp_path / "mulch"))

        d = _entry(
            id="mx-idempotent",
            name="test",
            description="File is services/test.py.",
            evidence={},
        )
        svc._write_entries("understand", [d])

        from prism_service.services import decision_evidence as de_module
        monkeypatch.setattr(
            de_module, "get_project",
            lambda p: type("_", (), {"memory_svc": svc})(),
        )

        # First apply
        result1 = decision_evidence.apply("test_project", ["mx-idempotent"])
        updated1 = svc.get_entry("mx-idempotent")
        evidence1 = updated1.evidence.copy()

        # Second apply (should be idempotent — evidence already present)
        result2 = decision_evidence.apply("test_project", ["mx-idempotent"])
        updated2 = svc.get_entry("mx-idempotent")
        evidence2 = updated2.evidence

        assert evidence1 == evidence2, "Second apply should not change existing evidence"
