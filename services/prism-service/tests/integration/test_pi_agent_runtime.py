"""Red suite — pi as PRISM's internal inference agent (task ac69ee28).

The pi toolkit (Node, pi-agent-core) becomes the internal agent runtime:
web/pi-runtime.mjs runs the agent loop on the local micro model with PRISM
tools bridged through /api/agent/tool; inference/pi_agent.py wraps it like
claude_cli.invoke; PRISM_REFLECTION_BACKEND=pi makes reflection agentic
(brain_search/memory_recall grounding before the verdict).

No Ollama, no live daemon: --check is offline, plumbing uses a fake runner.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
_RUNNER = _SERVICE_ROOT / "prism_service" / "web" / "pi-runtime.mjs"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node not installed")


def test_runner_check_offline():
    """AC-1: the runner validates pi imports + tool construction offline."""
    assert _RUNNER.exists(), f"runner missing: {_RUNNER}"
    proc = subprocess.run(
        [node, str(_RUNNER), "--check"],
        capture_output=True, text=True, timeout=120,
        cwd=str(_RUNNER.parent),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "ok" in proc.stdout.lower()


def test_pi_agent_invoke_plumbing(tmp_path, monkeypatch):
    """AC-2: pi_agent.invoke spawns the runner, feeds the job on stdin,
    parses the JSON result. Proven with a fake runner that echoes a canned
    payload derived from the job."""
    fake = tmp_path / "fake-runner.mjs"
    fake.write_text(
        """
let raw = "";
process.stdin.on("data", (c) => (raw += c));
process.stdin.on("end", () => {
  const job = JSON.parse(raw);
  process.stdout.write(JSON.stringify({
    ok: true,
    text: `verdict for: ${job.prompt}`,
    turns: 1,
    tools_used: [{ name: "memory_recall", ms: 12, ok: true }],
    ms: 34,
    tokens: 5,
    model: job.model,
  }));
});
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PRISM_PI_RUNNER", str(fake))
    monkeypatch.setenv("PRISM_LOCAL_LLM_MODEL", "stub-micro")

    from prism_service.inference import pi_agent

    out = pi_agent.invoke("ground this brief", system="expert", allowed_tools=("memory_recall",))
    assert out["ok"] is True
    assert out["text"] == "verdict for: ground this brief"
    assert out["tools_used"][0]["name"] == "memory_recall"
    assert out["model"] == "stub-micro"


def test_pi_agent_invoke_raises_on_garbage_runner(tmp_path, monkeypatch):
    fake = tmp_path / "broken-runner.mjs"
    fake.write_text('process.stdout.write("not json"); ', encoding="utf-8")
    monkeypatch.setenv("PRISM_PI_RUNNER", str(fake))

    from prism_service.inference import pi_agent

    with pytest.raises(pi_agent.PiRuntimeError):
        pi_agent.invoke("x")


def test_reflection_backend_pi_routes_through_pi_agent(monkeypatch):
    """AC-3: PRISM_REFLECTION_BACKEND=pi routes reflection through
    pi_agent.invoke; claude_cli must NOT run."""
    from prism_service.inference import claude_cli, pi_agent
    from prism_service.services import reflection_runner as rr

    monkeypatch.setenv("PRISM_REFLECTION_BACKEND", "pi")
    calls: list[dict] = []

    def fake_invoke(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return {"ok": True, "text": '{"worth_learning": false, "new_memories": []}',
                "turns": 2, "tools_used": [{"name": "memory_recall", "ms": 9, "ok": True}],
                "ms": 40, "tokens": 20, "model": "stub-micro"}
    monkeypatch.setattr(pi_agent, "invoke", fake_invoke)

    def forbidden(*a, **k):
        raise AssertionError("claude_cli.invoke must not run on the pi backend")
    monkeypatch.setattr(claude_cli, "invoke", forbidden)

    result = rr._invoke_backend(
        prompt="reflect", work_dir=".", plugin_dir=".",
        max_turns=5, allowed_tools=(), project="p", purpose="prism-reflect",
    )
    assert result.exit_code == 0
    assert "worth_learning" in (result.final_text() or "")
    assert result.duration_s >= 0
    assert calls, "pi_agent.invoke was not called"
    # The agentic backend passes the read tools so the model can ground.
    assert "brain_search" in (calls[0].get("allowed_tools") or ())
    assert "memory_recall" in (calls[0].get("allowed_tools") or ())
