"""Integration tests for the PI agent tool passthrough (api/agent.py).

Task 711d5235 (Omnipresent PI assistant in the left rail): the browser-side
pi agent does its own tool calling against PRISM's MCP tool surface through
ONE endpoint — POST /api/agent/tool {name, args} — which dispatches
in-process via mcp.tools.handle_tool, gated to a whitelist. AC-6: a
non-whitelisted tool name is rejected with 403.

Exercises the real FastAPI router via TestClient against an isolated
PRISM_DATA_DIR so no real project stores are touched.
"""

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    from prism_service.main import app

    c = TestClient(app)
    # get_project no longer creates on miss (d37193da): create the
    # test project explicitly through the documented affordance.
    c.post("/api/projects", json={"name": "agenttest"})
    return c


def test_whitelisted_tool_dispatches_in_process(client):
    """prism_status is whitelisted — the passthrough must return the real
    in-process handle_tool result (a JSON payload with doc counts)."""
    r = client.post(
        "/api/agent/tool",
        params={"project": "agenttest"},
        json={"name": "prism_status", "args": {}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("name") == "prism_status"
    # handle_tool returns the tool's JSON text; the route surfaces it parsed.
    assert "result" in body, body
    assert "docs" in str(body["result"]), body


def test_memory_store_then_recall_roundtrip(client):
    """memory_store + memory_recall are both whitelisted (the self-learning
    write path). A stored memory must be recallable through the same
    passthrough — proves real dispatch, not a mocked echo."""
    r = client.post(
        "/api/agent/tool",
        params={"project": "agenttest"},
        json={
            "name": "memory_store",
            "args": {
                "domain": "pi-agent",
                "name": "roundtrip-probe",
                "description": "PI panel roundtrip probe memory",
                "type": "convention",
                "classification": "tactical",
            },
        },
    )
    assert r.status_code == 200, r.text
    r2 = client.post(
        "/api/agent/tool",
        params={"project": "agenttest"},
        json={"name": "memory_recall", "args": {"query": "roundtrip probe"}},
    )
    assert r2.status_code == 200, r2.text
    assert "roundtrip probe" in str(r2.json().get("result")), r2.text


def test_non_whitelisted_tool_rejected_403(client):
    """AC-6 oracle: janitor_abandon is a real MCP tool but NOT whitelisted
    for the PI agent — the passthrough must refuse with 403."""
    r = client.post(
        "/api/agent/tool",
        params={"project": "agenttest"},
        json={"name": "janitor_abandon", "args": {"ticket_id": "x"}},
    )
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("name", [
    "janitor_check", "janitor_submit", "janitor_abandon",
    "prism_sync", "okf_index", "okf_get", "okf_graph",
    "prism_onboard", "register_claude_source",
])
def test_excluded_admin_tools_still_403(client, name):
    """AC-3 (task e70cdcda): the expert-surface widening is EXACT — the
    admin/destructive tools stay excluded even though the whitelist grew
    to the full 18-tool expert catalog."""
    r = client.post(
        "/api/agent/tool",
        params={"project": "agenttest"},
        json={"name": name, "args": {}},
    )
    assert r.status_code == 403, f"{name}: {r.text}"


def test_whitelist_is_exactly_the_expert_catalog():
    """Task e70cdcda FR-3: AGENT_TOOL_WHITELIST == the 18-name expert
    catalog, and the internal-only gate is retired (empty seam)."""
    from prism_service.api.agent import AGENT_TOOL_WHITELIST, INTERNAL_ONLY_TOOLS

    expected = {
        "brain_search", "brain_understand", "brain_find_symbol",
        "brain_outline", "brain_find_references", "brain_call_chain",
        "memory_recall", "memory_store", "memory_invalidate",
        "task_list", "task_next", "task_create", "task_update",
        "conductor_advance", "conductor_gate",
        "workflow_state", "context_bundle", "prism_status",
    }
    assert set(AGENT_TOOL_WHITELIST) == expected
    assert INTERNAL_ONLY_TOOLS == frozenset()


def test_unknown_tool_rejected_403(client):
    """A made-up tool name never reaches the dispatcher."""
    r = client.post(
        "/api/agent/tool",
        params={"project": "agenttest"},
        json={"name": "definitely_not_a_tool", "args": {}},
    )
    assert r.status_code == 403, r.text


def test_missing_name_rejected_422(client):
    r = client.post(
        "/api/agent/tool", params={"project": "agenttest"}, json={"args": {}}
    )
    assert r.status_code == 422, r.text


# ----------------------------------------------------------------------
# Task 4f76beb9 — machine-legible tool errors, never a 200-wrapped raw
# Python exception. The pi agent loop consumes this endpoint; an
# AttributeError string dressed as "result" is indistinguishable from a
# real tool result, so the micro-model treats stack noise as knowledge.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,args",
    [
        pytest.param("brain_search", {"query": {"nested": {"deep": [1]}}},
                     id="brain_search-dict"),
        pytest.param("brain_search", {"query": 12345}, id="brain_search-int"),
        pytest.param("memory_recall", {"query": {"x": 1}},
                     id="memory_recall-dict"),
    ],
)
def test_type_confused_args_return_machine_legible_error(client, tool, args):
    """AC-1/AC-2: the three filed repros. Type-confused args must come back
    as HTTP 200 {"name", "ok": false, "error": "<message>"} — the in-band
    dispatch error surfaced as a MACHINE-legible field, never a raw
    'Error: AttributeError: ...' string under "result"."""
    r = client.post(
        "/api/agent/tool",
        params={"project": "agenttest"},
        json={"name": tool, "args": args},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("name") == tool
    assert body.get("ok") is False, body
    assert isinstance(body.get("error"), str) and body["error"].strip(), body
    # The raw exception text must NOT masquerade as a tool result.
    assert "Error:" not in str(body.get("result", "")), body


def test_success_shape_carries_ok_true(client):
    """AC-3: a valid whitelisted call keeps its shape and gains ok=true so
    `ok` is a reliable success/error discriminator for the agent loop."""
    r = client.post(
        "/api/agent/tool",
        params={"project": "agenttest"},
        json={"name": "prism_status", "args": {}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True, body
    assert "result" in body and "error" not in body, body


def test_args_as_string_still_422(client):
    """AC-4: the existing args-shape validation is unchanged."""
    r = client.post(
        "/api/agent/tool",
        params={"project": "agenttest"},
        json={"name": "brain_search", "args": "not-an-object"},
    )
    assert r.status_code == 422, r.text


def test_dispatch_exception_returns_clean_500(client, monkeypatch):
    """AC-5: a truly exceptional path (the dispatcher itself raising, not an
    in-band tool error) is HTTP 500 with a clean detail naming the exception
    type + message — never a traceback body."""
    import prism_service.mcp.tools as mcp_tools

    async def boom(name, arguments, *, project_id="default"):
        raise RuntimeError("dispatcher exploded")

    monkeypatch.setattr(mcp_tools, "handle_tool", boom)
    r = client.post(
        "/api/agent/tool",
        params={"project": "agenttest"},
        json={"name": "prism_status", "args": {}},
    )
    assert r.status_code == 500, r.text
    detail = str(r.json().get("detail", ""))
    assert "RuntimeError" in detail and "dispatcher exploded" in detail, detail
    assert "Traceback" not in detail and "File \"" not in detail, detail


def test_error_behind_reflection_nudge_detected(client, monkeypatch):
    """AC-6: handle_tool may prepend the PRISM_REFLECTION_PENDING nudge
    header to the FIRST content — an in-band error behind it must still be
    detected as an error, not parsed/returned as a result."""
    import prism_service.mcp.tools as mcp_tools
    from mcp.types import TextContent

    nudged = (
        "⚠️ PRISM_REFLECTION_PENDING candidate=c1 task=t1\n"
        "Before continuing, spawn the `prism-reflect` subagent. Call "
        "`janitor_check` to fetch the brief, submit via `janitor_submit`.\n"
        "---\n"
        "Error: AttributeError: 'dict' object has no attribute 'split'"
    )

    async def nudged_error(name, arguments, *, project_id="default"):
        return [TextContent(type="text", text=nudged)]

    monkeypatch.setattr(mcp_tools, "handle_tool", nudged_error)
    r = client.post(
        "/api/agent/tool",
        params={"project": "agenttest"},
        json={"name": "brain_search", "args": {"query": "x"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is False, body
    assert "'dict' object has no attribute 'split'" in str(body.get("error")), body
