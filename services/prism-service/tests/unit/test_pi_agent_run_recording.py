"""RED scaffold — pi_agent.invoke() records every run into pi_run_log.

The recording seam is ADDITIVE: a successful runner job appends exactly
one backend=pi manifest row carrying the runner's tools_used receipts;
a spawn failure appends an ok=False row and still raises PiRuntimeError;
a broken ledger never breaks invoke() (never raises into the caller).

ALL FAIL today: pi_run_log does not exist and invoke() records nothing.
"""

from __future__ import annotations

import json
import subprocess

import pytest


@pytest.fixture
def isolated_runs_dir(tmp_path, monkeypatch):
    import prism_service.services.pi_run_log as prl
    runs_dir = tmp_path / "pi_runs"
    monkeypatch.setattr(prl, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(prl, "_MANIFEST", runs_dir / "manifest.jsonl")
    return runs_dir


@pytest.fixture
def fake_runner(tmp_path, monkeypatch):
    """Point PRISM_PI_RUNNER at an existing file and stub node discovery
    so invoke() reaches the subprocess seam without a real Node install."""
    runner = tmp_path / "fake-runtime.mjs"
    runner.write_text("// fake pi runner", encoding="utf-8")
    monkeypatch.setenv("PRISM_PI_RUNNER", str(runner))
    monkeypatch.setenv("PRISM_PI_NODE", "node-not-actually-spawned")
    return runner


def _completed(payload: dict, returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["node"], returncode=returncode,
        stdout=json.dumps(payload), stderr="")


RESULT = {
    "ok": True,
    "text": "grounded answer",
    "turns": 2,
    "tools_used": [{"name": "brain_search", "ms": 31.5, "ok": True}],
    "ms": 812.0,
    "tokens": 57,
    "model": "qwen3:0.6b",
}


def test_invoke_records_one_pi_row_with_receipts(
        isolated_runs_dir, fake_runner, monkeypatch):
    from prism_service.inference import pi_agent
    import prism_service.services.pi_run_log as prl

    monkeypatch.setattr(pi_agent.subprocess, "run",
                        lambda *a, **k: _completed(RESULT))
    out = pi_agent.invoke("prompt text", project="proj-y")
    assert out["ok"] is True

    runs = prl.list_recent(limit=10)
    assert len(runs) == 1, "exactly one manifest row per invoke"
    r = runs[0]
    assert r["backend"] == "pi"
    assert r["project"] == "proj-y"
    assert r["model"] == "qwen3:0.6b"
    assert r["tokens"] == 57
    assert r["turns"] == 2
    assert r["duration_ms"] == pytest.approx(812.0)
    assert r["prompt_chars"] == len("prompt text")
    assert r["tools_used"] == [{"name": "brain_search", "ms": 31.5, "ok": True}]
    assert r["ok"] is True


def test_invoke_purpose_explicit_and_inferred(
        isolated_runs_dir, fake_runner, monkeypatch):
    from prism_service.inference import pi_agent
    import prism_service.services.pi_run_log as prl

    monkeypatch.setattr(pi_agent.subprocess, "run",
                        lambda *a, **k: _completed(RESULT))

    # Explicit purpose wins.
    pi_agent.invoke("p1", purpose="panel-bridge")
    # Reflection is inferred from the reflection engine's system prompt
    # (reflection_runner is owned by another lane and passes no purpose).
    pi_agent.invoke("p2", system="You are PRISM's reflection engine and "
                                 "its memory/brain expert.")
    # No purpose, no reflection marker -> adhoc.
    pi_agent.invoke("p3")

    purposes = [r["purpose"] for r in prl.list_recent(limit=10)]
    assert purposes == ["adhoc", "reflect", "panel-bridge"]  # newest first


def test_invoke_model_failure_row_is_not_ok(
        isolated_runs_dir, fake_runner, monkeypatch):
    from prism_service.inference import pi_agent
    import prism_service.services.pi_run_log as prl

    failed = dict(RESULT, ok=False, text="", error="model refused")
    monkeypatch.setattr(pi_agent.subprocess, "run",
                        lambda *a, **k: _completed(failed))
    out = pi_agent.invoke("p")
    assert out["ok"] is False

    r = prl.list_recent(limit=1)[0]
    assert r["ok"] is False
    assert r["error"] == "model refused"


def test_invoke_spawn_failure_records_error_row_and_raises(
        isolated_runs_dir, fake_runner, monkeypatch):
    from prism_service.inference import pi_agent
    import prism_service.services.pi_run_log as prl

    def _boom(*a, **k):
        raise OSError("no node")
    monkeypatch.setattr(pi_agent.subprocess, "run", _boom)

    with pytest.raises(pi_agent.PiRuntimeError):
        pi_agent.invoke("p")

    runs = prl.list_recent(limit=10)
    assert len(runs) == 1
    assert runs[0]["ok"] is False
    assert "no node" in runs[0]["error"]


def test_broken_ledger_never_breaks_invoke(
        isolated_runs_dir, fake_runner, monkeypatch):
    """Recording is additive — a raising ledger must not raise into the
    caller path (AC-2)."""
    from prism_service.inference import pi_agent
    import prism_service.services.pi_run_log as prl

    monkeypatch.setattr(pi_agent.subprocess, "run",
                        lambda *a, **k: _completed(RESULT))

    def _explode(**kwargs):
        raise RuntimeError("ledger on fire")
    monkeypatch.setattr(prl, "record_run", _explode)

    out = pi_agent.invoke("p")
    assert out["ok"] is True
    assert out["text"] == "grounded answer"
