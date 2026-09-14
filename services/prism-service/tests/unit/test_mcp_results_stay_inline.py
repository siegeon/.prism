"""An interactive MCP result never exceeds what the engine carries inline
(task bb3d1f6a, 2026-09-14).

Live: brain_understand(query="prism_service") returned 118,077 chars; the
Claude Code engine spilled it to a tool-results file under its private
HOME and told the model to Read it back; the model mistyped the path and
burned 13 minutes of the single engine slot, twice, on implement_tasks.

Pins:
- AC-1  an oversized result becomes ONE json.loads()-able envelope naming
        the tool, the size, the limit, a narrowing hint, and the head.
- AC-2  a result under the bound passes through byte-identical.
- AC-3  an automation-profile caller is never bounded (it parses raw JSON).
- AC-4  PRISM_MCP_RESULT_MAX_CHARS overrides the bound; <= 0 disables it.
"""
from __future__ import annotations

import asyncio
import json

from mcp.types import TextContent

from prism_service.mcp import tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _fake_dispatch(text: str):
    def _dispatch(name, arguments, *, project_id="default"):
        return [TextContent(type="text", text=text)]
    return _dispatch


# AC-1 ------------------------------------------------------------------
def test_an_oversized_result_is_replaced_by_a_bounded_envelope(monkeypatch):
    big = json.dumps({"nodes": ["x" * 100] * 1500})  # ~150k chars
    monkeypatch.setattr(tools, "_dispatch_tool", _fake_dispatch(big))
    monkeypatch.setattr(tools, "_maybe_augment_with_nudge", lambda r, *, project_id: r)
    out = _run(tools.handle_tool("brain_understand", {"query": "prism_service"},
                                 project_id="prism"))
    assert len(out) == 1
    env = json.loads(out[0].text)
    assert env["truncated"] is True
    assert env["tool"] == "brain_understand"
    assert env["chars"] == len(big)
    assert env["limit"] == tools.RESULT_MAX_CHARS_DEFAULT
    assert "limit" in env["hint"] and "depth" in env["hint"]
    assert big.startswith(env["head"])
    assert len(out[0].text) <= tools.RESULT_MAX_CHARS_DEFAULT


# AC-2 ------------------------------------------------------------------
def test_a_small_result_passes_through_unchanged(monkeypatch):
    small = json.dumps({"ok": True, "hits": 3})
    monkeypatch.setattr(tools, "_dispatch_tool", _fake_dispatch(small))
    monkeypatch.setattr(tools, "_maybe_augment_with_nudge", lambda r, *, project_id: r)
    out = _run(tools.handle_tool("brain_search", {"query": "x"}, project_id="prism"))
    assert [p.text for p in out] == [small]


# AC-3 ------------------------------------------------------------------
def test_an_automation_caller_is_never_bounded(monkeypatch):
    big = "y" * 200_000
    monkeypatch.setattr(tools, "_dispatch_tool", _fake_dispatch(big))
    out = _run(tools.handle_tool("brain_understand", {}, project_id="prism",
                                 tool_profile="automation"))
    assert out[0].text == big


# AC-4 ------------------------------------------------------------------
def test_the_env_override_moves_or_disables_the_bound(monkeypatch):
    text = "z" * 5_000
    monkeypatch.setattr(tools, "_dispatch_tool", _fake_dispatch(text))
    monkeypatch.setattr(tools, "_maybe_augment_with_nudge", lambda r, *, project_id: r)
    monkeypatch.setenv("PRISM_MCP_RESULT_MAX_CHARS", "2000")
    out = _run(tools.handle_tool("brain_understand", {}, project_id="prism"))
    assert json.loads(out[0].text)["truncated"] is True
    monkeypatch.setenv("PRISM_MCP_RESULT_MAX_CHARS", "0")
    out = _run(tools.handle_tool("brain_understand", {}, project_id="prism"))
    assert out[0].text == text
