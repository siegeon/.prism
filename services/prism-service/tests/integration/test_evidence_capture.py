"""Integration tests for evidence_capture.py -- pins task 575ccf1e.

Covers AC-1..AC-5 from the story:
- AC-1/AC-2: assertion_source_for reads verbatim source via ast, "" on miss.
- AC-3: capture_walkthrough degrades gracefully against an unreachable URL.
- AC-4: browser-dependent happy path, guarded so the suite stays green
  where no browser is installed.
- AC-5: provenance is deterministic given an injected `now`.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from prism_service.services.evidence_capture import (
    assertion_source_for,
    capture_walkthrough,
    provenance,
)

WORKSPACE = Path(__file__).resolve().parents[2]
THIS_REL = "tests/integration/test_evidence_capture.py"


def _fixture_target(a, b):
    # marker fixture: assertion_source_for must return this verbatim.
    assert a + b == b + a
    return a + b


def test_assertion_source_for_returns_verbatim_source():
    node_id = f"{THIS_REL}::_fixture_target"
    src = assertion_source_for(node_id, str(WORKSPACE))
    assert "def _fixture_target(a, b):" in src
    assert "assert a + b == b + a" in src
    assert "return a + b" in src


def test_assertion_source_for_missing_node_returns_empty_string():
    node_id = f"{THIS_REL}::test_this_function_does_not_exist"
    assert assertion_source_for(node_id, str(WORKSPACE)) == ""


def test_assertion_source_for_missing_file_returns_empty_string():
    node_id = "tests/integration/does_not_exist.py::whatever"
    assert assertion_source_for(node_id, str(WORKSPACE)) == ""


def test_capture_walkthrough_degrades_gracefully_on_unreachable_url(tmp_path):
    result = capture_walkthrough(
        "http://127.0.0.1:59999",
        str(tmp_path),
        selector=None,
        video=False,
    )
    assert result["screenshot"] is None
    assert result["video"] is None
    assert result["error"] is not None


def test_capture_walkthrough_happy_path_data_url(tmp_path):
    pytest.importorskip("playwright")
    try:
        result = capture_walkthrough(
            "data:text/html,<html><body><h1 id='ok'>hi</h1></body></html>",
            str(tmp_path),
            selector="#ok",
            video=False,
        )
    except Exception:
        pytest.skip("chromium not launchable in this environment")
    if result["error"] is not None:
        pytest.skip(f"chromium not usable: {result['error']}")
    assert result["screenshot"] is not None
    assert Path(result["screenshot"]).is_file()


def test_provenance_is_deterministic_with_injected_now():
    fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    prov = provenance("v7.0.52", "abc123", now=fixed)
    assert prov["captured_by"] == "conductor-runner"
    assert prov["captured_at"] == fixed.isoformat()
    assert prov["build"] == "v7.0.52"
    assert prov["tree_sha"] == "abc123"
