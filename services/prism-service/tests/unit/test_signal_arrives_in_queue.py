"""A signal arrives and sits in the Queue (task a6858911).

Walking skeleton per owner's model (mx-0889e4): the Queue is where SIGNALS
arrive over their channel and are resolved against the ontology; a signal
is NOT a task -- it becomes one only when the owner acts in the app. Pins
SignalStore round-tripping, POST/GET /api/signals, and MCP signal_post,
and asserts none of these ever write a tasks row.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


@pytest.fixture
def project():
    """A throwaway project name under the suite-pinned PRISM_DATA_DIR
    (tests/conftest.py) -- unique per test so parallel runs never collide."""
    return f"signal-test-{uuid.uuid4().hex[:8]}"


def _tasks(project: str):
    from prism_service.project_context import get_project
    return get_project(project).task_svc.list()


# ── SignalStore round-trip ───────────────────────────────────────────────

def test_signal_store_round_trips_a_signal(project):
    from prism_service.models.signal import Signal
    from prism_service.services.signal_store import SignalStore

    store = SignalStore(project)
    signal = Signal(
        project=project, channel="slack", channel_ref="C123/456",
        subject="ping from slack", body="hello", sender="alice",
    )
    store.create(signal)
    got = store.get(signal.id)
    assert got is not None
    assert got.channel == "slack"
    assert got.channel_ref == "C123/456"
    assert got.subject == "ping from slack"
    assert got.state == "open"


# ── POST /api/signals ─────────────────────────────────────────────────────

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import signals as signals_api
    app = FastAPI()
    app.include_router(signals_api.router, prefix="/api/signals")
    return TestClient(app)


def test_post_signals_stores_and_returns_the_row(project):
    client = _client()
    r = client.post(
        "/api/signals", params={"project": project},
        json={"channel": "github", "channel_ref": "https://x/issues/1",
              "subject": "new issue", "body": "details", "sender": "bob"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["signal"]
    assert body["channel"] == "github"
    assert body["channel_ref"] == "https://x/issues/1"
    assert body["subject"] == "new issue"
    assert body["sender"] == "bob"
    assert body["state"] == "open"

    from prism_service.services.signal_store import SignalStore
    got = SignalStore(project).get(body["id"])
    assert got is not None and got.subject == "new issue"


def test_post_signals_unknown_channel_is_400(project):
    client = _client()
    r = client.post(
        "/api/signals", params={"project": project},
        json={"channel": "carrier-pigeon", "subject": "x"},
    )
    assert r.status_code == 400, r.text
    from prism_service.services.signal_store import SignalStore
    assert SignalStore(project).list() == []


def test_post_signals_blank_channel_defaults_to_ui(project):
    client = _client()
    r = client.post(
        "/api/signals", params={"project": project}, json={"subject": "x"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["signal"]["channel"] == "ui"


# ── GET /api/signals ──────────────────────────────────────────────────────

def test_get_signals_newest_first_and_state_filter(project):
    client = _client()
    for i, subj in enumerate(["first", "second", "third"]):
        client.post("/api/signals", params={"project": project},
                    json={"channel": "mcp", "subject": subj})

    from prism_service.services.signal_store import SignalStore
    store = SignalStore(project)
    all_signals = store.list()
    dropped = all_signals[0]
    store.update(dropped.id, state="dropped", drop_reason="dup")

    r = client.get("/api/signals", params={"project": project})
    subjects = [s["subject"] for s in r.json()["signals"]]
    assert subjects == ["third", "second", "first"]

    r_open = client.get("/api/signals", params={"project": project, "state": "open"})
    assert all(s["state"] == "open" for s in r_open.json()["signals"])
    assert len(r_open.json()["signals"]) == 2

    r_dropped = client.get("/api/signals", params={"project": project, "state": "dropped"})
    assert [s["id"] for s in r_dropped.json()["signals"]] == [dropped.id]


# ── MCP signal_post ────────────────────────────────────────────────────────

def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


def test_signal_post_tool_is_registered_and_interactive():
    from prism_service.mcp.tools import TOOLS, tools_for_profile
    names = {t.name for t in TOOLS}
    assert "signal_post" in names
    interactive = {t.name for t in tools_for_profile("interactive")}
    assert "signal_post" in interactive


def test_mcp_signal_post_defaults_channel_to_mcp_and_session_ref(project, monkeypatch):
    from prism_service.mcp import tools as mcp_tools
    monkeypatch.setattr(mcp_tools, "_resolve_real_session_id", lambda: "sess-signal-1")

    result = json.loads(_call("signal_post", {"subject": "from an agent"}, project))
    assert result["channel"] == "mcp"
    assert result["channel_ref"] == "sess-signal-1"

    from prism_service.services.signal_store import SignalStore
    got = SignalStore(project).get(result["id"])
    assert got is not None
    assert got.channel == "mcp"


def test_mcp_signal_post_honours_an_explicit_channel(project):
    result = json.loads(_call(
        "signal_post",
        {"subject": "relayed", "channel": "slack", "channel_ref": "C1/2"},
        project,
    ))
    assert result["channel"] == "slack"
    assert result["channel_ref"] == "C1/2"


def test_mcp_signal_post_refuses_an_unknown_channel(project):
    out = json.loads(_call(
        "signal_post", {"subject": "bogus", "channel": "carrier-pigeon"}, project))
    assert "error" in out


# ── No tasks row is ever created by any of this ───────────────────────────

def test_no_tasks_row_is_created_by_signal_intake(project):
    assert _tasks(project) == []

    client = _client()
    client.post("/api/signals", params={"project": project},
               json={"channel": "jira", "subject": "issue"})
    _call("signal_post", {"subject": "from mcp"}, project)

    assert _tasks(project) == []
