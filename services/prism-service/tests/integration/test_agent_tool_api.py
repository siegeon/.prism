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
                "content": "PI panel roundtrip probe memory",
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
