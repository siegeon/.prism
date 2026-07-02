"""Red suite — event-bus inference routes through env-gated backend seams
(task cba42d6b, claude-p-exit epic be898578).

event_handlers has the bus's two direct `claude -p` sites:
  * _summarize_haiku (:191) — novel memory.written summary, haiku-pinned,
    tool-less. Must route through memory_summary_worker._invoke_backend so
    PRISM_MEMORY_SUMMARY_BACKEND=local serves it from the local micro model
    (with a pi_runs audit-ledger row — task d1d4fe00: pi_runs is THE
    ledger for pi|local runs; claude_runs stays claude-only), while the
    default keeps the haiku pin.
  * _reflect_once (:319) — session.imported coalesced reflect. Must
    delegate to reflection_runner._invoke_backend so PRISM_REFLECTION_
    BACKEND=local|pi applies; default stays claude with the same args.

All stubs — pytest never needs Ollama or the claude CLI.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------
class _FakeMem:
    def __init__(self):
        self.updates: list[tuple[str, str]] = []

    def update_entry(self, entry_id, **kw):
        self.updates.append((entry_id, kw.get("summary", "")))


def _fake_ctx(tmp_path):
    return SimpleNamespace(_data_dir=tmp_path)


def _entry():
    return SimpleNamespace(id="m1", name="mem-name", description="mem-desc")


class _FakeCliResult:
    def __init__(self, text):
        self._text = text
        self.exit_code = 0
        self.run_id = "fake-claude"
        self.duration_s = 0.1

    def final_text(self):
        return self._text


@pytest.fixture
def isolated_runs_dir(tmp_path, monkeypatch):
    import prism_service.services.claude_run_log as crl
    runs_dir = tmp_path / "claude_runs"
    monkeypatch.setattr(crl, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(crl, "_MANIFEST", runs_dir / "manifest.jsonl")
    return runs_dir


@pytest.fixture
def forbid_claude(monkeypatch):
    from prism_service.inference import claude_cli

    def forbidden(*a, **k):
        raise AssertionError(
            "claude_cli.invoke must not run on a local backend")
    monkeypatch.setattr(claude_cli, "invoke", forbidden)


def _scores_db(tmp_path):
    db = str(tmp_path / "scores.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE consolidation_candidates ("
        " id TEXT PRIMARY KEY, task_id TEXT, session_id TEXT,"
        " scope_json TEXT, trigger TEXT, status TEXT, queued_at TEXT)"
    )
    conn.execute(
        "INSERT INTO consolidation_candidates "
        "(id, task_id, session_id, scope_json, trigger, status, queued_at) "
        "VALUES ('cand1', 't1', 's1', '{}', 'test', 'pending', '2026-01-01')"
    )
    conn.commit()
    conn.close()
    return db


# ----------------------------------------------------------------------
# AC-1 — novelty summary default path keeps the haiku pin via claude.
# ----------------------------------------------------------------------
def test_novelty_summary_default_stays_haiku_claude(tmp_path, monkeypatch):
    from prism_service.inference import claude_cli, local_llm
    from prism_service.services import event_handlers as eh

    monkeypatch.delenv("PRISM_MEMORY_SUMMARY_BACKEND", raising=False)
    monkeypatch.setattr(
        local_llm, "complete",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("local_llm must not run on the default path")))

    seen: list[dict] = []

    def fake_invoke(**kw):
        seen.append(kw)
        return _FakeCliResult("One clean sentence.")
    monkeypatch.setattr(claude_cli, "invoke", fake_invoke)

    mem = _FakeMem()
    eh._summarize_haiku(_fake_ctx(tmp_path), mem, _entry())

    assert mem.updates == [("m1", "One clean sentence.")]
    assert seen, "claude_cli.invoke was not called on the default path"
    kw = seen[0]
    assert kw["model"] == "haiku"          # criterion 7 pin preserved
    assert kw["max_turns"] == 1
    assert kw["allowed_tools"] == ()
    assert kw["purpose"] == "memory_summary"
    from prism_service.services.memory_summary_worker import render_prompt
    assert kw["prompt"] == render_prompt("mem-name", "mem-desc")


# ----------------------------------------------------------------------
# AC-2 — novelty summary local backend: no claude, pi_runs row recorded
# (task d1d4fe00: pi_runs owns pi|local; claude_runs stays claude-only).
# ----------------------------------------------------------------------
def test_novelty_summary_local_backend_routes_and_records(
        tmp_path, monkeypatch, isolated_runs_dir, forbid_claude):
    import io

    from prism_service.inference import local_llm
    import prism_service.services.pi_run_log as prl
    from prism_service.services import event_handlers as eh

    pi_dir = tmp_path / "pi_runs"
    monkeypatch.setattr(prl, "_RUNS_DIR", pi_dir)
    monkeypatch.setattr(prl, "_MANIFEST", pi_dir / "manifest.jsonl")

    monkeypatch.setenv("PRISM_MEMORY_SUMMARY_BACKEND", "local")
    monkeypatch.setenv("PRISM_LOCAL_LLM_MODEL", "stub-micro")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    payload = json.dumps({
        "choices": [{"message": {"content": "Local tile sentence."}}],
        "usage": {"prompt_tokens": 17, "completion_tokens": 6},
    }).encode()
    monkeypatch.setattr(local_llm.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(payload))

    mem = _FakeMem()
    eh._summarize_haiku(_fake_ctx(tmp_path), mem, _entry())

    assert mem.updates == [("m1", "Local tile sentence.")]
    rows = prl.list_recent(limit=10)
    assert len(rows) == 1, "local run must land in the pi_runs ledger"
    assert rows[0]["backend"] == "local"
    assert rows[0]["purpose"] == "memory_summary"
    assert rows[0]["input_tokens"] == 17
    assert rows[0]["output_tokens"] == 6
    assert not (isolated_runs_dir / "manifest.jsonl").exists(), \
        "claude_runs must stay claude-only — no local seam rows"


# ----------------------------------------------------------------------
# AC-3 — coalesced reflect default path stays claude with the same args.
# ----------------------------------------------------------------------
def test_reflect_once_default_stays_claude(tmp_path, monkeypatch):
    from prism_service.inference import claude_cli, local_llm
    from prism_service.services import event_handlers as eh
    from prism_service.services import reflection_runner as rr

    monkeypatch.delenv("PRISM_REFLECTION_BACKEND", raising=False)
    monkeypatch.setattr(
        local_llm, "complete",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("local_llm must not run on the default path")))

    seen: list[dict] = []
    verdict_json = '{"worth_learning": false, "new_memories": []}'

    def fake_invoke(**kw):
        seen.append(kw)
        return _FakeCliResult(verdict_json)
    monkeypatch.setattr(claude_cli, "invoke", fake_invoke)

    db = _scores_db(tmp_path)
    verdict = eh._reflect_once(
        "no-such-project", _fake_ctx(tmp_path), db, "cand1")

    assert verdict == {"worth_learning": False, "new_memories": []}
    assert seen, "claude_cli.invoke was not called on the default path"
    kw = seen[0]
    assert kw["max_turns"] == 15
    assert tuple(kw["allowed_tools"]) == tuple(rr.DEFAULT_REFLECT_TOOLS)
    assert kw["purpose"] == "prism-reflect"


# ----------------------------------------------------------------------
# AC-4 — coalesced reflect honors PRISM_REFLECTION_BACKEND=local.
# ----------------------------------------------------------------------
def test_reflect_once_local_backend(tmp_path, monkeypatch, forbid_claude):
    from prism_service.inference import local_llm
    from prism_service.services import event_handlers as eh

    monkeypatch.setenv("PRISM_REFLECTION_BACKEND", "local")
    calls: list[dict] = []

    def fake_complete(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return {"text": '{"worth_learning": true, "new_memories": []}',
                "ms": 8.0, "tokens": 15,
                "input_tokens": 40, "output_tokens": 15}
    monkeypatch.setattr(local_llm, "complete", fake_complete)

    db = _scores_db(tmp_path)
    verdict = eh._reflect_once(
        "no-such-project", _fake_ctx(tmp_path), db, "cand1")

    assert verdict == {"worth_learning": True, "new_memories": []}
    assert calls, "local_llm.complete was not reached via the reflection seam"
    # The reflect brief itself must be the prompt (contract unchanged).
    assert "reflection sub-agent" in calls[0]["prompt"]
