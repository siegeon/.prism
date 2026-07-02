"""RED scaffold — local_llm.complete() records every run into pi_run_log.

Plain completions against the keyless local endpoint are internal
inference too: each complete() appends one backend=local manifest row;
an unreachable endpoint appends an ok=False row and re-raises the
original error unchanged; the new purpose/project kwargs are additive
(defaults keep every existing caller untouched).

ALL FAIL today: pi_run_log does not exist and complete() records nothing.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest


@pytest.fixture
def isolated_runs_dir(tmp_path, monkeypatch):
    import prism_service.services.pi_run_log as prl
    runs_dir = tmp_path / "pi_runs"
    monkeypatch.setattr(prl, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(prl, "_MANIFEST", runs_dir / "manifest.jsonl")
    return runs_dir


class _FakeResponse(io.BytesIO):
    """Context-manager response like urlopen's."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen_payload():
    return json.dumps({
        "choices": [{"message": {"content": "verdict text"}}],
        "usage": {"completion_tokens": 21},
    }).encode()


def test_complete_records_one_local_row(isolated_runs_dir, monkeypatch):
    from prism_service.inference import local_llm
    import prism_service.services.pi_run_log as prl

    monkeypatch.setattr(
        local_llm.urllib.request, "urlopen",
        lambda req, timeout=0: _FakeResponse(_fake_urlopen_payload()))

    out = local_llm.complete("brief text", model="qwen3:1.7b",
                             project="proj-z")
    assert out["text"] == "verdict text"
    assert out["tokens"] == 21

    runs = prl.list_recent(limit=10)
    assert len(runs) == 1
    r = runs[0]
    assert r["backend"] == "local"
    assert r["model"] == "qwen3:1.7b"
    assert r["project"] == "proj-z"
    assert r["tokens"] == 21
    assert r["prompt_chars"] == len("brief text")
    assert r["tools_used"] == []  # tool-less by design
    assert r["ok"] is True
    assert r["duration_ms"] >= 0


def test_complete_purpose_inferred_for_reflection(
        isolated_runs_dir, monkeypatch):
    from prism_service.inference import local_llm
    import prism_service.services.pi_run_log as prl

    monkeypatch.setattr(
        local_llm.urllib.request, "urlopen",
        lambda req, timeout=0: _FakeResponse(_fake_urlopen_payload()))

    local_llm.complete("b1", system="You are PRISM's reflection engine.")
    local_llm.complete("b2")
    local_llm.complete("b3", purpose="panel-bridge")

    purposes = [r["purpose"] for r in prl.list_recent(limit=10)]
    assert purposes == ["panel-bridge", "adhoc", "reflect"]  # newest first


def test_complete_unreachable_records_error_and_reraises(
        isolated_runs_dir, monkeypatch):
    from prism_service.inference import local_llm
    import prism_service.services.pi_run_log as prl

    def _refused(req, timeout=0):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(local_llm.urllib.request, "urlopen", _refused)

    with pytest.raises(urllib.error.URLError):
        local_llm.complete("brief")

    runs = prl.list_recent(limit=10)
    assert len(runs) == 1
    assert runs[0]["backend"] == "local"
    assert runs[0]["ok"] is False
    assert "connection refused" in runs[0]["error"]


def test_broken_ledger_never_breaks_complete(isolated_runs_dir, monkeypatch):
    from prism_service.inference import local_llm
    import prism_service.services.pi_run_log as prl

    monkeypatch.setattr(
        local_llm.urllib.request, "urlopen",
        lambda req, timeout=0: _FakeResponse(_fake_urlopen_payload()))

    def _explode(**kwargs):
        raise RuntimeError("ledger on fire")
    monkeypatch.setattr(prl, "record_run", _explode)

    out = local_llm.complete("brief")
    assert out["text"] == "verdict text"
