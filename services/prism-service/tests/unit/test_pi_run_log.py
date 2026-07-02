"""RED scaffold — pi/local internal-inference run ledger (visual audit).

Every internal pi/local inference run (pi_agent.invoke jobs incl.
reflection backend=pi, local_llm.complete calls) must land in an
auditable ledger: services/pi_run_log.py, mirroring the claude_run_log
pattern (append-only DATA_DIR/pi_runs/manifest.jsonl, bounded trim,
best-effort record that never raises, list_recent/get_run).

ALL FAIL today: prism_service.services.pi_run_log does not exist.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def isolated_runs_dir(tmp_path, monkeypatch):
    """Repoint pi_run_log at tmp_path — mirror of the claude_run_log
    fixture so manifest writes never touch a live data volume."""
    import prism_service.services.pi_run_log as prl
    runs_dir = tmp_path / "pi_runs"
    monkeypatch.setattr(prl, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(prl, "_MANIFEST", runs_dir / "manifest.jsonl")
    return runs_dir


def _record(prl, **overrides):
    kwargs = dict(
        backend="pi",
        model="qwen3:0.6b",
        purpose="reflect",
        project="proj-x",
        prompt_chars=1234,
        tools_used=[{"name": "brain_search", "ms": 40.2, "ok": True},
                    {"name": "memory_recall", "ms": 12.0, "ok": False}],
        duration_ms=1567.8,
        tokens=42,
        turns=3,
        ok=True,
        error="",
    )
    kwargs.update(overrides)
    return prl.record_run(**kwargs)


def test_record_and_list_round_trip(isolated_runs_dir):
    import prism_service.services.pi_run_log as prl

    run_id = _record(prl)
    assert run_id, "record_run must return the allocated run_id"

    runs = prl.list_recent(limit=10)
    assert len(runs) == 1
    r = runs[0]
    assert r["run_id"] == run_id
    assert r["backend"] == "pi"
    assert r["model"] == "qwen3:0.6b"
    assert r["purpose"] == "reflect"
    assert r["project"] == "proj-x"
    assert r["prompt_chars"] == 1234
    assert r["duration_ms"] == pytest.approx(1567.8)
    assert r["tokens"] == 42
    assert r["turns"] == 3
    assert r["ok"] is True
    assert r["error"] == ""
    # tools_used receipts ride the manifest row (no stream file for pi).
    assert r["tools_used"] == [
        {"name": "brain_search", "ms": 40.2, "ok": True},
        {"name": "memory_recall", "ms": 12.0, "ok": False},
    ]
    assert isinstance(r["ts"], float) and r["ts"] > 0


def test_list_recent_newest_first_and_project_filter(isolated_runs_dir):
    import prism_service.services.pi_run_log as prl

    first = _record(prl, project="alpha", model="m-1")
    second = _record(prl, project="beta", model="m-2")
    third = _record(prl, project="alpha", model="m-3")

    runs = prl.list_recent(limit=10)
    assert [r["run_id"] for r in runs] == [third, second, first]

    alpha = prl.list_recent(limit=10, project="alpha")
    assert [r["run_id"] for r in alpha] == [third, first]
    assert prl.list_recent(limit=10, project="never-seen") == []
    # limit slices AFTER newest-first ordering.
    assert [r["run_id"] for r in prl.list_recent(limit=1)] == [third]


def test_get_run_returns_full_entry(isolated_runs_dir):
    import prism_service.services.pi_run_log as prl

    run_id = _record(prl, backend="local", ok=False,
                     error="URLError: connection refused", tools_used=[])
    entry = prl.get_run(run_id)
    assert entry is not None
    assert entry["backend"] == "local"
    assert entry["ok"] is False
    assert entry["error"] == "URLError: connection refused"
    assert prl.get_run("does-not-exist") is None


def test_trim_to_limit_drops_oldest(isolated_runs_dir, monkeypatch):
    import prism_service.services.pi_run_log as prl
    monkeypatch.setattr(prl, "RUN_LOG_LIMIT", 2)

    ids = [_record(prl, model=f"m-{i}") for i in range(4)]
    runs = prl.list_recent(limit=10)
    assert len(runs) == 2
    assert [r["run_id"] for r in runs] == [ids[3], ids[2]]


def test_record_run_failure_does_not_raise(isolated_runs_dir, monkeypatch):
    """Ledger writes are best-effort — a broken manifest path must never
    raise into the inference caller path (AC-1)."""
    import prism_service.services.pi_run_log as prl
    bad = isolated_runs_dir / "is_a_dir"
    bad.mkdir(parents=True)
    monkeypatch.setattr(prl, "_MANIFEST", bad)

    # Should not raise; returns None on failure.
    assert _record(prl) is None


def test_error_text_is_truncated(isolated_runs_dir):
    import prism_service.services.pi_run_log as prl

    run_id = _record(prl, ok=False, error="x" * 8192)
    entry = prl.get_run(run_id)
    assert entry is not None
    assert len(entry["error"]) <= 1024
