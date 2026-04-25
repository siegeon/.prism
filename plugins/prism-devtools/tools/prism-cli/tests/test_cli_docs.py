"""
AC-traced tests for CLI documentation update story.
PLAT-0000-cli-documentation-update

Story: Update README and docs/index.md for PRISM Dashboard CLI (v2.5.0)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PRISM_ROOT = Path(__file__).parents[3]  # plugins/prism-devtools/
README = PRISM_ROOT / "README.md"
DOCS_INDEX = PRISM_ROOT / "docs" / "index.md"


@pytest.fixture(scope="module")
def readme_text() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def docs_index_text() -> str:
    return DOCS_INDEX.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC-1: README version header says 2.5.0
# ---------------------------------------------------------------------------


class TestAC1ReadmeVersion:
    """AC-1: README version header is 2.5.0"""

    def test_readme_exists(self) -> None:
        assert README.exists(), f"README not found at {README}"

    def test_readme_version_is_250(self, readme_text: str) -> None:
        assert "2.5.0" in readme_text, (
            "README.md does not mention version 2.5.0 — update the version header"
        )

    def test_readme_version_header_format(self, readme_text: str) -> None:
        # Should have **Version 2.5.0** somewhere in the header area
        assert re.search(r"Version 2\.5\.0", readme_text), (
            "README.md missing 'Version 2.5.0' in version header"
        )


# ---------------------------------------------------------------------------
# AC-2: README What's New has v2.5.0 subsection with all five features
# ---------------------------------------------------------------------------


class TestAC2ReadmeWhatsNew:
    """AC-2: README What's New contains v2.5.0 with all five features"""

    def test_readme_has_250_whats_new_section(self, readme_text: str) -> None:
        assert re.search(r"###\s+Version 2\.5\.0", readme_text), (
            "README.md missing '### Version 2.5.0' subsection under What's New"
        )

    def test_readme_250_mentions_dashboard_tui(self, readme_text: str) -> None:
        assert "PRISM Dashboard TUI" in readme_text, (
            "README v2.5.0 section missing 'PRISM Dashboard TUI'"
        )

    def test_readme_250_mentions_snapshot_mode(self, readme_text: str) -> None:
        assert "Snapshot Mode" in readme_text or "--snapshot" in readme_text, (
            "README v2.5.0 section missing CLI Snapshot Mode"
        )

    def test_readme_250_mentions_prism_dashboard_command(self, readme_text: str) -> None:
        assert "/prism-dashboard" in readme_text, (
            "README v2.5.0 section missing '/prism-dashboard' command"
        )

    def test_readme_250_mentions_validate_cli_command(self, readme_text: str) -> None:
        assert "/validate-cli" in readme_text, (
            "README v2.5.0 section missing '/validate-cli' command"
        )

    def test_readme_250_mentions_branch_correlation(self, readme_text: str) -> None:
        assert "branch correlation" in readme_text.lower() or (
            "session-story-branch" in readme_text
        ), (
            "README v2.5.0 section missing session-story-branch correlation"
        )


# ---------------------------------------------------------------------------
# AC-3: README Workflow Automation lists /prism-dashboard and /validate-cli
# ---------------------------------------------------------------------------


class TestAC3ReadmeWorkflowAutomation:
    """AC-3: README Workflow Automation section lists new commands"""

    def test_readme_workflow_automation_mentions_prism_dashboard(
        self, readme_text: str
    ) -> None:
        # /prism-dashboard should appear in the Workflow Automation feature list
        assert "/prism-dashboard" in readme_text, (
            "README Workflow Automation section missing '/prism-dashboard'"
        )

    def test_readme_workflow_automation_mentions_validate_cli(
        self, readme_text: str
    ) -> None:
        assert "/validate-cli" in readme_text, (
            "README Workflow Automation section missing '/validate-cli'"
        )


# ---------------------------------------------------------------------------
# AC-4: README Directory Structure includes tools/ entry
# ---------------------------------------------------------------------------


class TestAC4ReadmeDirectoryStructure:
    """AC-4: README Directory Structure section has tools/ entry"""

    def test_readme_directory_structure_has_tools(self, readme_text: str) -> None:
        assert "tools/" in readme_text, (
            "README Directory Structure section missing 'tools/' entry for prism-cli"
        )


# ---------------------------------------------------------------------------
# AC-5: docs/index.md version is 2.5.0 and Last Updated is 2026-03-03
# ---------------------------------------------------------------------------


class TestAC5DocsIndexVersion:
    """AC-5: docs/index.md version header is 2.5.0"""

    def test_docs_index_exists(self) -> None:
        assert DOCS_INDEX.exists(), f"docs/index.md not found at {DOCS_INDEX}"

    def test_docs_index_version_is_250(self, docs_index_text: str) -> None:
        assert "2.5.0" in docs_index_text, (
            "docs/index.md does not mention version 2.5.0"
        )

    def test_docs_index_last_updated(self, docs_index_text: str) -> None:
        assert "2026-03-03" in docs_index_text, (
            "docs/index.md Last Updated date not updated to 2026-03-03"
        )


# ---------------------------------------------------------------------------
# AC-6: docs/index.md Commands section says 15 commands and lists new ones
# ---------------------------------------------------------------------------


class TestAC6DocsIndexCommands:
    """AC-6: docs/index.md Commands section has correct count and new commands"""

    def test_docs_index_command_count_is_15(self, docs_index_text: str) -> None:
        assert "15 slash commands" in docs_index_text, (
            "docs/index.md still says '13 slash commands' — should be 15"
        )

    def test_docs_index_lists_prism_dashboard(self, docs_index_text: str) -> None:
        assert "/prism-dashboard" in docs_index_text, (
            "docs/index.md Commands section missing '/prism-dashboard'"
        )

    def test_docs_index_lists_validate_cli(self, docs_index_text: str) -> None:
        assert "/validate-cli" in docs_index_text, (
            "docs/index.md Commands section missing '/validate-cli'"
        )


# ---------------------------------------------------------------------------
# AC-7: docs/index.md System Status has v2.5.0 entry
# ---------------------------------------------------------------------------


class TestAC7DocsIndexSystemStatus:
    """AC-7: docs/index.md System Status section has v2.5.0 entry"""

    def test_docs_index_status_has_250_header(self, docs_index_text: str) -> None:
        assert re.search(r"v2\.5\.0|2\.5\.0", docs_index_text), (
            "docs/index.md System Status section has no v2.5.0 entry"
        )

    def test_docs_index_status_250_mentions_dashboard(
        self, docs_index_text: str
    ) -> None:
        assert "Dashboard" in docs_index_text or "dashboard" in docs_index_text, (
            "docs/index.md v2.5.0 status entry missing Dashboard mention"
        )
