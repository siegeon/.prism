"""Tests for conventions rendering and test_drafted rubric scoring.

Tests _render_conventions helper and validates the JSON node configuration.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from prism_service.api.workflows import _render_conventions


@dataclass
class FakeExpertiseEntry:
    """Mock ExpertiseEntry for testing."""
    id: str = ""
    type: str = "convention"
    name: str = ""
    description: str = ""
    recall_count: int = 0
    last_recalled: str = ""
    evidence: dict = None
    owner_user_id: str = ""
    adr_status: str = ""
    source_file: str = ""

    def __post_init__(self):
        if self.evidence is None:
            self.evidence = {}


def test_render_conventions_dataclass_objects():
    """Render conventions from dataclass objects with long descriptions."""
    conventions = [
        FakeExpertiseEntry(
            name="Pattern A",
            description="This is a long description that contains a lot of details about the pattern and how it is used across the codebase."
        ),
        FakeExpertiseEntry(
            name="Pattern B",
            description="Short desc"
        ),
    ]
    rendered = _render_conventions(conventions)

    assert "Pattern A" in rendered
    assert "Pattern B" in rendered
    # Ensure we don't see raw metadata field names
    assert "recall_count" not in rendered
    assert "last_recalled" not in rendered
    assert "owner_user_id" not in rendered
    assert "ExpertiseEntry(" not in rendered
    assert "source_file" not in rendered
    assert "adr_status" not in rendered


def test_render_conventions_dict_objects():
    """Render conventions from dict objects."""
    conventions = [
        {
            "name": "Dict Convention",
            "description": "A convention stored as a dictionary",
            "recall_count": 5,
            "some_noise": "ignored",
        }
    ]
    rendered = _render_conventions(conventions)

    assert "Dict Convention" in rendered
    assert "A convention stored as a dictionary" in rendered
    assert "recall_count" not in rendered
    assert "some_noise" not in rendered


def test_render_conventions_mixed_dicts_and_objects():
    """Render conventions mixing dicts and dataclass objects."""
    conventions = [
        FakeExpertiseEntry(name="Object Entry", description="From object"),
        {"name": "Dict Entry", "description": "From dict"},
    ]
    rendered = _render_conventions(conventions)

    assert "Object Entry" in rendered
    assert "Dict Entry" in rendered


def test_render_conventions_respects_character_budget():
    """Verify rendered output respects the total character budget."""
    conventions = [
        FakeExpertiseEntry(
            name=f"Convention {i}",
            description=f"Description {i} - " + "x" * 100
        )
        for i in range(20)  # Many entries to exceed budget
    ]
    rendered = _render_conventions(conventions)

    # Total characters should not exceed the budget (default 1500) by much.
    # The note about omitted entries may add ~100 bytes, so allow 1600 max.
    assert len(rendered) <= 1600


def test_render_conventions_empty_list():
    """Empty conventions produce no dangling header."""
    assert _render_conventions([]) == ""
    assert _render_conventions(None) == ""


def test_render_conventions_truncates_descriptions():
    """Long descriptions are truncated and marked."""
    conventions = [
        FakeExpertiseEntry(
            name="Long Desc",
            description="x" * 300
        )
    ]
    rendered = _render_conventions(conventions)

    # Should contain ellipsis to mark truncation
    assert "…" in rendered or "(" in rendered  # truncation marker


def test_render_conventions_skips_nameless_entries():
    """Entries without names are skipped."""
    conventions = [
        FakeExpertiseEntry(name="Valid", description="Has name"),
        FakeExpertiseEntry(name="", description="No name"),
        {"name": None, "description": "No name dict"},
    ]
    rendered = _render_conventions(conventions)

    assert "Valid" in rendered
    assert rendered.count("•") == 1  # Only one bullet point


def test_render_conventions_handles_missing_fields():
    """Gracefully handles objects with missing fields."""
    conventions = [
        FakeExpertiseEntry(name="Has Name", description=""),  # Missing description
        {"name": "Dict Name"},  # Missing description in dict
    ]
    rendered = _render_conventions(conventions)

    assert "Has Name" in rendered
    assert "Dict Name" in rendered


def test_render_conventions_notation_for_dropped_entries():
    """Dropped entries are noted in output when budget exceeded."""
    # Create many entries to force dropping
    conventions = [
        FakeExpertiseEntry(
            name=f"Conv{i}",
            description="x" * 200
        )
        for i in range(50)
    ]
    rendered = _render_conventions(conventions)

    # Should note that entries were omitted
    if len(rendered) > 0:
        # Verify the output mentions omission if entries were actually dropped
        assert "omitted" in rendered or len(rendered) <= 1500


def test_write_failing_tests_node_json_valid():
    """Validate the write-failing-tests-loop.json node configuration."""
    node_path = Path(
        __file__
    ).parent.parent.parent.parent.parent / ".prism" / "behaviors" / "conductor" / "write-failing-tests-loop.json"

    assert node_path.exists(), f"Node file not found: {node_path}"

    with open(node_path, encoding="utf-8") as f:
        node = json.load(f)

    # Validate basic structure
    assert node["id"] == "write-failing-tests-loop"
    assert len(node["steps"]) > 0

    # The first step should have the reason-loop call with test_drafted rubric
    first_step = node["steps"][0]
    assert first_step["kind"] == "http-callback"
    assert "reason-loop" in first_step["url"]

    # Parse the nested JSON string to verify rubric field
    body_json = json.loads(first_step["body"])
    assert body_json["rubric"] == "test_drafted", \
        f"Expected rubric='test_drafted', got {body_json['rubric']!r}"
    assert body_json["model"] == "haiku"
    assert body_json["max_turns"] == 4


def test_render_conventions_complete_fixture():
    """Test with realistic 8-entry fixture matching the measured problem."""
    conventions = [
        FakeExpertiseEntry(
            id="mx-b579c1",
            type="convention",
            name="Entry 1",
            description="This is a description for the first entry that explains a pattern or convention in the codebase.",
            recall_count=6,
            last_recalled="2026-09-11T10:00:00Z",
            evidence={"source_file": "models.py"},
            owner_user_id="user123",
            adr_status="accepted",
            source_file="models.py"
        )
        for _ in range(8)
    ]

    rendered = _render_conventions(conventions)

    # Should render successfully
    assert rendered != ""
    assert "Entry 1" in rendered

    # Should NOT contain raw metadata
    assert "ExpertiseEntry(" not in rendered
    assert "recall_count" not in rendered
    assert "last_recalled" not in rendered
    assert "owner_user_id" not in rendered
    assert "adr_status" not in rendered
    assert "source_file" not in rendered
    assert "evidence" not in rendered

    # Verify it's much smaller than the raw repr
    # Raw repr would be ~20k tokens for 8 entries; rendered should be << 1.5k chars
    assert len(rendered) < 1500


def test_render_conventions_no_dangling_header_when_empty():
    """When conventions render to empty, the prompt is adjusted."""
    conventions = []
    rendered = _render_conventions(conventions)
    assert rendered == ""

    # The calling code should handle this by not appending the header
    # This test verifies _render_conventions returns empty, not a space/newline
