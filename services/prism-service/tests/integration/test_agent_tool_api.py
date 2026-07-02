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

    return TestClient(app)


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
