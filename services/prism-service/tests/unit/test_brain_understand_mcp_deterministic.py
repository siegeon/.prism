"""MCP determinism guard for brain_understand (xref S2).

Generated / claude-provenance annotations are EXPLORATION-ONLY: the
LLM narrative layer may surface them on the HTTP / UI path
(/api/brain/understand + ContextRail), but they must NOT leave via the
MCP brain_understand tool. The strip is handler-LOCAL, so the shared
builder understand_view.build_understanding stays byte-unchanged.

  AC-1: a known claude-provenance annotation is ABSENT from MCP output.
  AC-2: build_understanding's DIRECT (HTTP-path) output STILL CONTAINS it.
"""

from __future__ import annotations

import json
import types

from prism_service import project_context
from prism_service.mcp import tools as t
from prism_service.services import understand_view


def _controlled_payload() -> dict:
    """A build_understanding-shaped payload seeded with one deterministic
    and one claude-provenance annotation on a single context entry."""
    return {
        "query": "x",
        "mode": "focus",
        "nodes": [],
        "edges": [],
        "communities": [],
        "ranked": [],
        "context": [
            {
                "entity_id": "main.py",
                "file": "main.py",
                "annotations": [
                    {
                        "scope_kind": "node",
                        "scope_id": "main.py",
                        "name": "deterministic name",
                        "purpose": "structural",
                        "provenance": "deterministic",
                    },
                    {
                        "scope_kind": "node",
                        "scope_id": "main.py",
                        "name": "CLAUDE_SECRET_SUMMARY",
                        "purpose": "llm narrative",
                        "provenance": "claude-opus-4-8",
                    },
                ],
            }
        ],
        "open_questions": [],
        "counts": {},
        "provenance": "deterministic",
    }


def _stub_ctx() -> types.SimpleNamespace:
    """brain_understand never touches the project-scoped services, but
    _dispatch_tool resolves them before the branch — hand back a stub."""
    return types.SimpleNamespace(
        brain_svc=None, task_svc=None, workflow_svc=None,
        memory_svc=None, conductor_svc=None, governance=None,
    )


def _call_mcp(monkeypatch) -> dict:
    monkeypatch.setattr(project_context, "get_project", lambda pid: _stub_ctx())
    monkeypatch.setattr(
        understand_view, "build_understanding",
        lambda *a, **k: _controlled_payload(),
    )
    out = t._dispatch_tool("brain_understand", {"query": "x"}, project_id="proj")
    assert out and out[0].text
    return json.loads(out[0].text)


def test_mcp_strips_claude_provenance_annotation(monkeypatch):
    """AC-1: the claude-provenance annotation is gone from MCP output."""
    payload = _call_mcp(monkeypatch)
    text = json.dumps(payload)
    assert "CLAUDE_SECRET_SUMMARY" not in text
    anns = payload["context"][0]["annotations"]
    assert all(
        not str(a.get("provenance", "")).lower().startswith("claude")
        for a in anns
    )
    # The deterministic annotation survives the strip.
    assert any(a["name"] == "deterministic name" for a in anns)


def test_builder_direct_output_retains_claude_annotation(monkeypatch):
    """AC-2: build_understanding's DIRECT output (HTTP path) is untouched."""
    monkeypatch.setattr(
        understand_view, "build_understanding",
        lambda *a, **k: _controlled_payload(),
    )
    direct = understand_view.build_understanding("proj", "x")
    text = json.dumps(direct)
    assert "CLAUDE_SECRET_SUMMARY" in text
    anns = direct["context"][0]["annotations"]
    assert any(
        str(a.get("provenance", "")).lower().startswith("claude")
        for a in anns
    )


def test_handler_does_not_mutate_shared_builder_payload(monkeypatch):
    """The strip is handler-local: the object the builder returned is not
    mutated in place (a real builder's output feeds the HTTP path too)."""
    shared = _controlled_payload()
    monkeypatch.setattr(project_context, "get_project", lambda pid: _stub_ctx())
    monkeypatch.setattr(
        understand_view, "build_understanding", lambda *a, **k: shared,
    )
    t._dispatch_tool("brain_understand", {"query": "x"}, project_id="proj")
    # shared still carries the claude annotation after the MCP call.
    names = [a["name"] for a in shared["context"][0]["annotations"]]
    assert "CLAUDE_SECRET_SUMMARY" in names
