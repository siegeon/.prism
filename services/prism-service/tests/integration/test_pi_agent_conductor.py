"""Red suite — pi agent drives the conductor state machine (task 9f20b605).

Phase 2 of the pi internal agent (phase 1: ac69ee28). The pi runtime tool
bridge gains the conductor surface — task_update, conductor_advance,
conductor_gate — so a local micro-model agent job can be DIRECTED at SDLC
work: read a task (task_list by id), author/patch plan_doc sections,
advance the per-task state machine, and propose gate evidence.

SAFETY (FR-3): the three mutating conductor tools are whitelisted for
INTERNAL AGENT CALLERS ONLY — POST /api/agent/tool refuses them with 403
unless the body carries internal=true. The browser PI panel never sends
the flag, so its reachable surface is unchanged. The subprocess runner
(pi-runtime.mjs) sends internal=true on every bridged call (FR-4).

Offline by construction: no Ollama, no live daemon — runner via --check,
HTTP surface via TestClient against an isolated PRISM_DATA_DIR.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
_RUNNER = _SERVICE_ROOT / "prism_service" / "web" / "pi-runtime.mjs"

_CONDUCTOR_TOOLS = ("task_update", "conductor_advance", "conductor_gate")

node = shutil.which("node")


# ---------------------------------------------------------------- runner


@pytest.mark.skipif(node is None, reason="node not installed")
def test_runner_check_lists_conductor_tools():
    """AC-1: --check constructs the conductor tool defs alongside the
    phase-1 set — the whitelist names are the REAL MCP tool names."""
    proc = subprocess.run(
        [node, str(_RUNNER), "--check"],
        capture_output=True, text=True, timeout=120,
        cwd=str(_RUNNER.parent),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out = json.loads(proc.stdout)
    assert out.get("ok") is True
    tools = out.get("tools") or []
    for name in _CONDUCTOR_TOOLS:
        assert name in tools, f"{name} missing from runner tool set: {tools}"


@pytest.mark.skipif(node is None, reason="node not installed")
def test_runner_bridge_body_carries_internal_flag():
    """FR-4: the runner is an internal caller by construction — the body it
    POSTs to /api/agent/tool carries internal=true (the same helper builds
    the body for --check and for live execute)."""
    proc = subprocess.run(
        [node, str(_RUNNER), "--check"],
        capture_output=True, text=True, timeout=120,
        cwd=str(_RUNNER.parent),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out = json.loads(proc.stdout)
    body = out.get("bridge_body") or {}
    assert body.get("internal") is True, (
        f"bridge body must carry internal=true for the internal-only "
        f"conductor tools: {body}"
    )
    assert body.get("name"), body
    assert isinstance(body.get("args"), dict), body


# ------------------------------------------------------------ pi_agent seam


def test_pi_agent_exports_conductor_toolset():
    """The named toolset seam a directed SDLC job composes from."""
    from prism_service.inference import pi_agent

    assert hasattr(pi_agent, "CONDUCTOR_TOOLS"), (
        "pi_agent.CONDUCTOR_TOOLS toolset seam missing"
    )
    assert set(_CONDUCTOR_TOOLS) <= set(pi_agent.CONDUCTOR_TOOLS)
    # A directed job also needs to READ the task it is driving.
    assert "task_list" in pi_agent.CONDUCTOR_TOOLS
    # The default surface stays read-only — conductor tools are opt-in.
    assert not set(_CONDUCTOR_TOOLS) & set(pi_agent.DEFAULT_TOOLS)


# ----------------------------------------------------------- HTTP surface


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    from prism_service.main import app

    return TestClient(app)


@pytest.mark.parametrize("name", _CONDUCTOR_TOOLS)
def test_conductor_tools_require_internal_flag(client, name):
    """AC-2 (refusal half): the three conductor tools are whitelisted but
    gated — without internal=true the passthrough must refuse with 403 so
    the browser panel surface is unchanged."""
    r = client.post(
        "/api/agent/tool",
        params={"project": "pisdlc"},
        json={"name": name, "args": {"id": "whatever"}},
    )
    assert r.status_code == 403, r.text
    assert "internal" in r.json().get("detail", "").lower(), r.text


@pytest.mark.parametrize("name", _CONDUCTOR_TOOLS)
def test_conductor_tools_reject_falsy_internal_flag(client, name):
    """internal must be literally true — a truthy-looking string or false
    does not open the gate."""
    for flag in (False, "true", 1):
        r = client.post(
            "/api/agent/tool",
            params={"project": "pisdlc"},
            json={"name": name, "args": {"id": "whatever"}, "internal": flag},
        )
        assert r.status_code == 403, f"internal={flag!r}: {r.text}"


def _create_task(client, title):
    r = client.post(
        "/api/agent/tool",
        params={"project": "pisdlc"},
        json={"name": "task_create", "args": {"title": title}},
    )
    assert r.status_code == 200, r.text
    task = r.json()["result"]
    assert task.get("id"), task
    return task["id"]


def test_task_update_with_internal_flag_dispatches(client):
    """AC-2 (dispatch half): with internal=true, task_update reaches the
    real handle_tool — a plan_doc round-trips onto an isolated task store."""
    tid = _create_task(client, "pi conductor probe")
    r = client.post(
        "/api/agent/tool",
        params={"project": "pisdlc"},
        json={
            "name": "task_update",
            "args": {"id": tid, "plan_doc": "## Summary\n\npi-authored plan"},
            "internal": True,
        },
    )
    assert r.status_code == 200, r.text
    r2 = client.post(
        "/api/agent/tool",
        params={"project": "pisdlc"},
        json={"name": "task_list", "args": {"id": tid, "fields": ["id", "plan_doc"]}},
    )
    assert r2.status_code == 200, r2.text
    rows = r2.json()["result"]
    assert rows and rows[0]["plan_doc"].startswith("## Summary"), rows


def test_conductor_advance_with_internal_flag_dispatches(client):
    """conductor_advance enters the workflow at review_previous_notes —
    proves the real ConductorService ran, not a mocked echo."""
    tid = _create_task(client, "pi conductor advance probe")
    r = client.post(
        "/api/agent/tool",
        params={"project": "pisdlc"},
        json={
            "name": "conductor_advance",
            "args": {"id": tid, "session_id": "pi-agent-test"},
            "internal": True,
        },
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result.get("ok") is True, result
    assert result.get("to_step") == "review_previous_notes", result


def test_conductor_gate_with_internal_flag_dispatches(client):
    """conductor_gate dispatches for real: on a task NOT sitting on a gate
    the service refuses in-band (ok=False, 'not currently on a gate') —
    which proves the call went through the whitelist to the conductor."""
    tid = _create_task(client, "pi conductor gate probe")
    r = client.post(
        "/api/agent/tool",
        params={"project": "pisdlc"},
        json={
            "name": "conductor_gate",
            "args": {"id": tid, "action": "approve",
                     "reason": "probe evidence", "session_id": "pi-agent-test"},
            "internal": True,
        },
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result.get("ok") is False, result
    assert "gate" in str(result.get("reason", "")).lower(), result


def test_read_tools_still_work_without_internal_flag(client):
    """NFR-1 regression guard: the phase-1 read surface is unchanged —
    no flag needed for the panel's whitelisted read tools."""
    r = client.post(
        "/api/agent/tool",
        params={"project": "pisdlc"},
        json={"name": "prism_status", "args": {}},
    )
    assert r.status_code == 200, r.text
