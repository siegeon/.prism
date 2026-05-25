"""Regression tests for analyzer_runner — preamble strip + classify.

`_strip_preamble_to_heading` was added to fix a false-failure bug where
onboarding_writer landed in the queue as `failed` even though Claude
exited 0 and emitted valid onboarding markdown. The model writes a
conversational lead-in like "Now I have everything I need. Emitting
the markdown." before the actual `# Heading`, and the classifier's
`payload.startswith("#")` check refused it as not-parseable.

The fix landed in v5.2.6 with zero test coverage. These tests pin the
behavior so a future refactor (or the v6.0.0 → v5.3.x pivot) doesn't
silently regress it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from prism_service.inference import analyzer_runner as ar


@dataclass
class _FakeResult:
    """Minimal stand-in for ClaudeCliResult — only the fields _classify
    actually reads."""
    exit_code: int = 0
    parsed_events: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# _strip_preamble_to_heading
# ---------------------------------------------------------------------------

def test_strip_preamble_drops_prose_before_h1():
    text = (
        "Now I have everything I need. Emitting the onboarding markdown.\n"
        "\n"
        "# Onboarding — prism\n"
        "\n"
        "## Section\n"
        "body"
    )
    out = ar._strip_preamble_to_heading(text)
    assert out.startswith("# Onboarding")
    assert "Emitting the onboarding markdown" not in out


def test_strip_preamble_returns_text_unchanged_when_h1_at_start():
    text = "# Already a heading\n\nbody"
    assert ar._strip_preamble_to_heading(text) == "# Already a heading\n\nbody"


def test_strip_preamble_returns_text_when_no_h1():
    """Without a top-level heading, leave the text alone — the classifier
    will mark it failed, which is the right outcome for a malformed
    payload."""
    text = "Just prose, no heading at all."
    assert ar._strip_preamble_to_heading(text) == text


def test_strip_preamble_handles_empty():
    assert ar._strip_preamble_to_heading("") == ""


def test_strip_preamble_handles_indented_h1():
    """lstrip() handling — Claude occasionally indents its emitted
    markdown by a tab or a few spaces. The strip should still find the
    heading."""
    text = "preamble\n    # Indented heading\nbody"
    out = ar._strip_preamble_to_heading(text)
    # The matched line is kept verbatim (including the indent)
    assert "# Indented heading" in out
    assert "preamble" not in out


# ---------------------------------------------------------------------------
# _classify — markdown payload (onboarding_writer)
# ---------------------------------------------------------------------------

def test_clean_markdown_classifies_complete():
    """The post-strip payload (what _extract_payload returns for
    onboarding_writer) starts with `#` and gets exit 0 — must be
    complete."""
    payload = "# Onboarding\n\n## What this project is\n\nbody"
    assert ar._classify(_FakeResult(exit_code=0), payload) == "complete"


def test_markdown_with_unstripped_preamble_classifies_failed():
    """The classifier alone (without the strip) still rejects a
    preamble-led payload. This is the failure mode the v5.2.6 strip
    addressed — locking it in so anyone removing the strip sees the
    regression immediately."""
    payload = "Now I have what I need.\n\n# Onboarding\n\nbody"
    assert ar._classify(_FakeResult(exit_code=0), payload) == "failed"


def test_markdown_exit_nonzero_with_valid_payload_is_partial():
    """When Claude hit max-turns (exit 1) but still emitted parseable
    markdown, persist as partial — the artifact is useful enough to
    keep, just not complete."""
    payload = "# Onboarding\n\nbody"
    assert ar._classify(_FakeResult(exit_code=1), payload) == "partial"


def test_markdown_pure_prose_no_heading_classifies_failed():
    payload = "I tried but got confused and gave up without writing markdown."
    assert ar._classify(_FakeResult(exit_code=0), payload) == "failed"


# ---------------------------------------------------------------------------
# _classify — JSON dict payload (tour, layers, domains)
# ---------------------------------------------------------------------------

def test_dict_with_schema_key_complete():
    payload = {"schema": "tour_builder_v1", "steps": []}
    assert ar._classify(_FakeResult(exit_code=0), payload) == "complete"


def test_dict_with_layers_key_complete():
    payload = {"layers": [{"id": "x"}]}
    assert ar._classify(_FakeResult(exit_code=0), payload) == "complete"


def test_dict_with_domains_key_complete():
    payload = {"domains": [{"id": "x"}]}
    assert ar._classify(_FakeResult(exit_code=0), payload) == "complete"


def test_dict_with_steps_key_complete():
    payload = {"steps": [{"title": "step 1"}]}
    assert ar._classify(_FakeResult(exit_code=0), payload) == "complete"


def test_dict_with_error_key_failed():
    """_extract_payload's JSON-parse fallback returns
    {"raw": "...", "error": "not_valid_json"}. That must classify as
    failed, not be silently accepted via the dict path."""
    payload = {"raw": "junk", "error": "not_valid_json"}
    assert ar._classify(_FakeResult(exit_code=0), payload) == "failed"


def test_dict_missing_expected_keys_failed():
    """A dict that's neither schema-shaped nor an error envelope fails."""
    payload = {"random": "thing"}
    assert ar._classify(_FakeResult(exit_code=0), payload) == "failed"


def test_dict_with_exit_nonzero_partial():
    payload = {"schema": "tour_builder_v1", "steps": []}
    assert ar._classify(_FakeResult(exit_code=1), payload) == "partial"
