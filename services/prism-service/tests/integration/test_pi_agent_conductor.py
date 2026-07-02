"""Red suite — pi agent drives the conductor state machine (task 9f20b605).

Phase 2 of the pi internal agent (phase 1: ac69ee28). The pi runtime tool
bridge gains the conductor surface — task_update, conductor_advance,
conductor_gate — so a local micro-model agent job can be DIRECTED at SDLC
work: read a task (task_list by id), author/patch plan_doc sections,
advance the per-task state machine, and propose gate evidence.

SAFETY history: 9f20b605 gated the three mutating conductor tools to
INTERNAL callers (403 without internal=true). Task e70cdcda DELIBERATELY
retired that gate per owner directive — PI IS the orchestrator, so the
panel drives the SDLC too; INTERNAL_ONLY_TOOLS is now an empty seam and
the runner still sends internal=true harmlessly on every bridged call.
Task e70cdcda also ships PI pre-loaded as the PRISM expert: ONE shared
module (web/pi-expert.mjs) sources the system prompt + 18-tool catalog.

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

# The full PRISM-expert catalog (task e70cdcda): PI ships pre-loaded as the
# PRISM expert — one shared module (web/pi-expert.mjs) sources the system
# prompt and this 18-tool surface for BOTH the runner and the rail panel.
_EXPERT_CATALOG = (
    "brain_search", "brain_understand", "brain_find_symbol", "brain_outline",
    "brain_find_references", "brain_call_chain",
    "memory_recall", "memory_store", "memory_invalidate",
    "task_list", "task_next", "task_create", "task_update",
    "conductor_advance", "conductor_gate",
    "workflow_state", "context_bundle", "prism_status",
)

_EXPERT_MODULE = _SERVICE_ROOT / "prism_service" / "web" / "pi-expert.mjs"

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
def test_runner_check_lists_full_expert_catalog():
    """AC-1 (task e70cdcda): the runner ships pre-loaded as the PRISM
    expert — --check constructs the FULL 18-tool catalog sourced from the
    shared pi-expert.mjs module."""
    proc = subprocess.run(
        [node, str(_RUNNER), "--check"],
        capture_output=True, text=True, timeout=120,
        cwd=str(_RUNNER.parent),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out = json.loads(proc.stdout)
    tools = set(out.get("tools") or [])
    missing = set(_EXPERT_CATALOG) - tools
    assert not missing, f"expert catalog tools missing from runner: {sorted(missing)}"
    assert len(tools) == len(_EXPERT_CATALOG), (
        f"runner tool set drifted from the expert catalog: {sorted(tools)}"
    )


@pytest.mark.skipif(node is None, reason="node not installed")
def test_runner_check_reports_expert_prompt():
    """AC-5 (task e70cdcda): the runner defaults an empty job.system to the
    shared EXPERT_SYSTEM_PROMPT — --check surfaces its length as proof the
    constant is wired."""
    proc = subprocess.run(
        [node, str(_RUNNER), "--check"],
        capture_output=True, text=True, timeout=120,
        cwd=str(_RUNNER.parent),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out = json.loads(proc.stdout)
    assert out.get("expert_prompt_chars", 0) > 500, (
        f"expert prompt missing/too small: {out.get('expert_prompt_chars')}"
    )


@pytest.mark.skipif(node is None, reason="node not installed")
def test_pi_expert_module_parses():
    """AC-4 (task e70cdcda): pi-expert.mjs is the ONE shared source — it
    parses as an ES module and pi-runtime.mjs imports its catalog instead
    of carrying a local TOOL_DEFS literal."""
    assert _EXPERT_MODULE.exists(), f"shared expert module missing: {_EXPERT_MODULE}"
    proc = subprocess.run(
        [node, "--check", str(_EXPERT_MODULE)],
        capture_output=True, text=True, timeout=60,
        cwd=str(_EXPERT_MODULE.parent),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    runtime_src = _RUNNER.read_text(encoding="utf-8")
    assert "pi-expert.mjs" in runtime_src, (
        "pi-runtime.mjs must import the shared pi-expert.mjs module"
    )
    assert "const TOOL_DEFS = {" not in runtime_src, (
        "pi-runtime.mjs must not duplicate the tool catalog locally"
    )


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


def test_pi_agent_exports_expert_toolset():
    """Task e70cdcda: the FULL expert catalog is a named toolset seam —
    DEFAULT_TOOLS stays lean for reflection, EXPERT_TOOLS is the whole
    pre-loaded PRISM surface."""
    from prism_service.inference import pi_agent

    assert hasattr(pi_agent, "EXPERT_TOOLS"), (
        "pi_agent.EXPERT_TOOLS expert toolset seam missing"
    )
    assert set(pi_agent.EXPERT_TOOLS) == set(_EXPERT_CATALOG)
    # Reflection default stays lean — the expert surface is opt-in.
    assert set(pi_agent.DEFAULT_TOOLS) == {"brain_search", "memory_recall"}


# ----------------------------------------------------------- HTTP surface


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    from prism_service.main import app

    c = TestClient(app)
    # get_project no longer creates on miss (d37193da): create the
    # test project explicitly through the documented affordance.
    c.post("/api/projects", json={"name": "pisdlc"})
    return c


# Task e70cdcda DELIBERATELY flips the old 403-without-internal contract:
# per owner directive PI IS the orchestrator, so task_update /
# conductor_advance / conductor_gate are panel-reachable WITHOUT the
# internal=true body flag (INTERNAL_ONLY_TOOLS is now an empty seam).
@pytest.mark.parametrize("name", _CONDUCTOR_TOOLS)
def test_conductor_tools_allowed_without_internal_flag(client, name):
    """AC-2 (task e70cdcda): the conductor tools dispatch for the panel —
    no internal flag needed; the call reaches the real handle_tool (an
    unknown-task refusal is IN-BAND ok=False, not HTTP 403)."""
    r = client.post(
        "/api/agent/tool",
        params={"project": "pisdlc"},
        json={"name": name, "args": {"id": "no-such-task"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Task 4f76beb9 refined the passthrough contract: a dispatch that
    # produces an in-band error (e.g. conductor_gate KeyError on missing
    # task_id) now surfaces as {ok: false, error} instead of stack noise
    # under "result". Either shape proves the tool DISPATCHED (never 403).
    assert body.get("result") is not None or (
        body.get("ok") is False and body.get("error")
    ), r.text


@pytest.mark.parametrize("name", sorted(
    {"brain_understand", "brain_find_symbol", "brain_outline",
     "brain_find_references", "brain_call_chain", "memory_invalidate",
     "task_next", "workflow_state", "context_bundle"}))
def test_expert_surface_tools_whitelisted(client, name):
    """Task e70cdcda: the widened expert surface dispatches through the
    passthrough (200 + real handle_tool payload, never a 403)."""
    args = {
        "brain_find_symbol": {"name": "probe"},
        "brain_outline": {"source_file": "nope.py"},
        "brain_find_references": {"name": "probe"},
        "brain_call_chain": {"entity": "probe"},
        "memory_invalidate": {"memory_id": "mx-000000"},
    }.get(name, {})
    r = client.post(
        "/api/agent/tool",
        params={"project": "pisdlc"},
        json={"name": name, "args": args},
    )
    assert r.status_code == 200, f"{name}: {r.text}"


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
