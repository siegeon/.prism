"""drift_worker's per-project backoff must stop re-sweeping a project that
keeps embedding zero files, and reset to immediate eligibility the moment
it embeds something real again (task: livehang round 6).

Even with per-file content hashing (brain_engine.py), a project that
stays "in use" continuously got swept every ~5s forever -- cheap per
pass once hashing is in, but "cheap x every 5 seconds forever" is still
real, sustained CPU (observed live: ~147% sustained on a checkout with
ongoing real, uncommitted edits)."""

from __future__ import annotations

import time

from prism_service.services import drift_worker, project_activity


class _StubBrain:
    def __init__(self, embedded_sequence, **kw):
        self._seq = list(embedded_sequence)
        self.calls = 0

    def incremental_reindex(self, repo_path=None, stats=None):
        self.calls += 1
        embedded = self._seq.pop(0) if self._seq else 0
        if stats is not None:
            stats["candidates"] = max(embedded, 1)
            stats["changed"] = embedded
            stats["embedded"] = embedded
        return embedded


class _StubCtx:
    def __init__(self, data_dir):
        self._data_dir = data_dir

        class _T:
            def active_ids(self_inner):
                return []

        self.task_svc = _T()


def setup_function(_fn):
    drift_worker.reset_for_tests()
    project_activity._last_seen.clear()
    project_activity._last_request_at = 0.0


def _patch(monkeypatch, tmp_path, embedded_sequence):
    repo = tmp_path / "repo"
    repo.mkdir()
    stub = _StubBrain(embedded_sequence)

    def fake_get_all_projects():
        return ["prism"]

    def fake_get_project(pid):
        d = tmp_path / "data"
        d.mkdir(exist_ok=True)
        return _StubCtx(d)

    def fake_source_path(pid):
        return str(repo)

    import prism_service.project_context as pc
    import prism_service.services.claude_transcripts as ct
    import prism_service.engines.brain_engine as be

    monkeypatch.setattr(pc, "get_all_projects", fake_get_all_projects)
    monkeypatch.setattr(pc, "get_project", fake_get_project)
    monkeypatch.setattr(ct, "_project_source_path", fake_source_path)
    monkeypatch.setattr(be, "Brain", lambda **kw: stub)
    monkeypatch.setattr(drift_worker, "IDLE_GATE_S", 0.01)
    monkeypatch.setattr(drift_worker, "BACKOFF_MIN_S", 60.0)
    monkeypatch.setattr(drift_worker, "BACKOFF_MAX_S", 900.0)
    return stub


def _tick(monkeypatch):
    project_activity.mark_seen("prism")
    time.sleep(0.02)  # clear the (shrunk) idle gate
    return drift_worker.sweep_once()


def test_a_zero_change_pass_backs_off_the_next_sweep(tmp_path, monkeypatch):
    stub = _patch(monkeypatch, tmp_path, embedded_sequence=[0, 0])

    first = _tick(monkeypatch)
    assert len(first) == 1 and stub.calls == 1, f"got {first!r}"

    # Immediately after a zero-change pass, the SAME project must not be
    # swept again yet -- backoff pushed its next eligible time out.
    second = _tick(monkeypatch)
    assert second == [], (
        f"a project that just embedded zero files must not be re-swept "
        f"on the very next tick -- got {second!r} (brain.calls={stub.calls})")
    assert stub.calls == 1, (
        f"the backed-off project's Brain must not even be asked to "
        f"reindex again; got {stub.calls} calls")


def test_a_real_change_resets_backoff_to_immediate(tmp_path, monkeypatch):
    stub = _patch(monkeypatch, tmp_path, embedded_sequence=[0, 3, 0])

    _tick(monkeypatch)  # embeds 0 -> backs off
    assert drift_worker._backoff.get("prism") is not None

    # Force the backoff window open early (simulating it having elapsed)
    # so we can observe the SECOND pass, which embeds something real.
    drift_worker._backoff["prism"]["next_at"] = time.monotonic() - 1
    second = _tick(monkeypatch)
    assert len(second) == 1, f"got {second!r}"
    assert drift_worker._backoff.get("prism") is None, (
        "a pass that embeds something real must reset backoff to "
        "immediate eligibility, not merely shorten it")

    # Immediately after that real change, the project is eligible again
    # (no backoff at all) -- the NEXT pass runs right away.
    third = _tick(monkeypatch)
    assert len(third) == 1, (
        f"a project must be swept again right away after a pass that "
        f"found a real change (backoff was reset); got {third!r}")
