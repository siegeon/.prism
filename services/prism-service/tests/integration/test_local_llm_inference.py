"""Red suite — micro-LLM internal inference for self-learning (task ecb90f7e).

PRISM's self-learning loop (reflection -> memory) must be directable at a
FREE local micro model instead of the claude CLI: a new OpenAI-compatible
client `prism_service.inference.local_llm` (no API key — INV-1 intact) and
an env-gated backend switch in reflection_runner
(PRISM_REFLECTION_BACKEND=local + PRISM_LOCAL_LLM_MODEL/_URL).

Tests run against a stdlib stub server — pytest must never require Ollama.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
_REPO_ROOT = _SERVICE_ROOT.parent.parent


class _StubHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible /chat/completions endpoint."""

    seen: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen.append({"path": self.path, "body": body})
        payload = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "hello world"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 7},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):  # silence
        pass


@pytest.fixture()
def stub_server():
    _StubHandler.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _StubHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}/v1"
    finally:
        srv.shutdown()


@pytest.mark.daemon_exclusive  # task 279ef52d: binds a real HTTP server + thread; flakes in-daemon
def test_complete_roundtrips_against_stub(stub_server):
    """AC-3: local_llm.complete hits <base_url>/chat/completions and returns
    {text, ms, tokens} — keyless, stdlib-only."""
    from prism_service.inference import local_llm

    out = local_llm.complete(
        "say hi", model="stub-model", base_url=stub_server, max_tokens=32,
    )
    assert out["text"] == "hello world"
    assert out["tokens"] == 7
    assert out["ms"] >= 0
    req = _StubHandler.seen[-1]
    assert req["path"].endswith("/chat/completions")
    assert req["body"]["model"] == "stub-model"
    assert req["body"]["messages"][-1]["content"] == "say hi"


@pytest.mark.daemon_exclusive  # task 279ef52d: binds a real HTTP server + thread; flakes in-daemon
def test_json_mode_and_system_prompt(stub_server):
    from prism_service.inference import local_llm

    local_llm.complete(
        "verdict please", model="stub-model", base_url=stub_server,
        system="You are the reflection engine.", json_mode=True,
    )
    body = _StubHandler.seen[-1]["body"]
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"
    assert "reflection engine" in body["messages"][0]["content"]


def test_reflection_backend_env_routes_local(monkeypatch):
    """AC-4: PRISM_REFLECTION_BACKEND=local routes reflection inference
    through local_llm; claude_cli.invoke must NOT be touched."""
    from prism_service.inference import claude_cli
    from prism_service.services import reflection_runner as rr

    monkeypatch.setenv("PRISM_REFLECTION_BACKEND", "local")
    monkeypatch.setenv("PRISM_LOCAL_LLM_MODEL", "stub-micro")

    calls: list[dict] = []

    def fake_complete(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return {"text": '{"worth_learning": false, "new_memories": []}',
                "ms": 5, "tokens": 12}

    from prism_service.inference import local_llm
    monkeypatch.setattr(local_llm, "complete", fake_complete)

    def forbidden(*a, **k):
        raise AssertionError("claude_cli.invoke must not run on the local backend")
    monkeypatch.setattr(claude_cli, "invoke", forbidden)

    result = rr._invoke_backend(
        prompt="reflect on this", work_dir=".", plugin_dir=".",
        max_turns=3, allowed_tools=(), project="p", purpose="prism-reflect",
    )
    assert result.exit_code == 0
    assert "worth_learning" in (result.final_text() or "")
    assert calls and calls[0]["model"] == "stub-micro"


def test_reflection_backend_default_stays_claude(monkeypatch):
    """Without the env opt-in the seam still delegates to claude_cli."""
    from prism_service.inference import claude_cli
    from prism_service.services import reflection_runner as rr

    monkeypatch.delenv("PRISM_REFLECTION_BACKEND", raising=False)
    hit: list[dict] = []

    class _FakeResult:
        exit_code = 0
        run_id = "fake"
        def final_text(self):
            return "{}"

    def fake_invoke(**kw):
        hit.append(kw)
        return _FakeResult()
    monkeypatch.setattr(claude_cli, "invoke", fake_invoke)

    result = rr._invoke_backend(
        prompt="reflect", work_dir=".", plugin_dir=".",
        max_turns=3, allowed_tools=(), project="p", purpose="prism-reflect",
    )
    assert result.final_text() == "{}"
    assert hit and hit[0]["purpose"] == "prism-reflect"


def test_bench_harness_self_check():
    """FR-1: the benchmark harness exists and validates its own task
    definitions without needing Ollama (--check)."""
    script = _REPO_ROOT / "benchmarks" / "micro_llm_selflearn" / "run.py"
    assert script.exists(), f"harness missing: {script}"
    proc = subprocess.run(
        [sys.executable, str(script), "--check"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
