"""brain_call_chain must accept ``name`` as an alias for ``entity`` so an
agent that standardizes on ``name`` across brain_find_symbol /
brain_find_references / brain_call_chain never hits a validation error
(task 0d2d5aeb).

RED before the fix: the dispatch reads ``arguments["entity"]`` (KeyError on a
name-only call, so call_chain is never reached) and the inputSchema hard-
requires ``entity`` with no ``name`` property.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_PID = "test-callchain-alias"


@pytest.fixture
def project(tmp_path):
    from prism_service import config as cfg
    original = cfg.PROJECTS_DIR
    cfg.PROJECTS_DIR = tmp_path / "projects"
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    # get_project no longer creates on miss (d37193da) — seed explicitly.
    cfg.project_data_dir(_PID)
    from prism_service import project_context as pc
    pc._contexts.clear()
    yield _PID
    cfg.PROJECTS_DIR = original
    pc._contexts.clear()


def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))


def _capture_call_chain(project_id, monkeypatch):
    """Stub brain_svc.call_chain to capture the kwargs the dispatch passes."""
    from prism_service.project_context import get_project
    ctx = get_project(project_id)
    captured: dict = {}

    def fake_call_chain(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(ctx.brain_svc, "call_chain", fake_call_chain)
    return captured


def test_name_alias_reaches_call_chain(project, monkeypatch):  # AC-1
    captured = _capture_call_chain(project, monkeypatch)
    _call("brain_call_chain", {"name": "foo"}, project)
    assert captured.get("entity") == "foo", (
        "name alias must supply the entity identifier when entity is absent"
    )


def test_entity_still_works(project, monkeypatch):  # AC-2 (back-compat)
    captured = _capture_call_chain(project, monkeypatch)
    _call("brain_call_chain", {"entity": "bar"}, project)
    assert captured.get("entity") == "bar"


def test_entity_wins_when_both_supplied(project, monkeypatch):  # FR-2
    captured = _capture_call_chain(project, monkeypatch)
    _call("brain_call_chain", {"entity": "keep", "name": "drop"}, project)
    assert captured.get("entity") == "keep"


def test_missing_both_returns_validation_error(project, monkeypatch):  # FR-3
    captured = _capture_call_chain(project, monkeypatch)
    result = _call("brain_call_chain", {}, project)
    # Neither param → call_chain must NOT be invoked, and the caller gets a
    # clear textual error rather than an opaque KeyError.
    assert captured == {}
    text = result[0].text.lower()
    assert "entity" in text or "name" in text
    assert "keyerror" not in text


def test_schema_exposes_name_and_not_entity_only():  # AC-3
    from prism_service.mcp.tools import TOOLS
    tool = next(t for t in TOOLS if t.name == "brain_call_chain")
    props = tool.inputSchema["properties"]
    assert "name" in props, "inputSchema must advertise the canonical `name`"
    assert "entity" in props, "the `entity` alias must remain for back-compat"
    assert "entity" not in tool.inputSchema.get("required", []), (
        "entity must no longer be hard-required on its own"
    )
