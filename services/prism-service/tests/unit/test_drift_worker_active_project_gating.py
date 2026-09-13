"""drift_worker.sweep_once() must reindex ONLY a project someone is
actually using right now (task: livehang round 4).

Owner directive (2026-09-13, verbatim intent): "we only care about the
project we have open... stop doing work just for fun... the whole thing
is supposed to be fast and buttery smooth." The OLD start_drift_timer
swept EVERY tracked project (get_all_projects(), ~30 slugs on the live
instance, including junk ones nobody opens) on a fixed cadence, and its
very first tick after any restart fired unconditionally -- pegging the
CPU at 150-180% and starving every other thread's access to the GIL for
20-30 minutes on every restart (confirmed live: /api/workflows, /api/
conductor/state and /api/work/graph all hung >20s during that window
while /api/version stayed at 10ms).

This suite stubs Brain entirely (a real one loads an embedding model --
slow, and irrelevant to the gating/scheduling logic being pinned here);
brain_engine.py's incremental_reindex(repo_path=...) itself -- the OTHER
half of this fix, the per-project baseline scoping -- is pinned
separately in test_brain_incremental_reindex_scopes_to_repo_path.py."""

from __future__ import annotations

import time

from prism_service.services import drift_worker, project_activity


class _StubBrain:
    def __init__(self, **kw):
        self.calls: list[str | None] = []

    def incremental_reindex(self, repo_path=None, stats=None):
        self.calls.append(repo_path)
        if stats is not None:
            stats["candidates"] = 0
            stats["changed"] = 0
            stats["embedded"] = 0
        return 0


class _StubTaskSvc:
    def __init__(self, active_ids=()):
        self._active_ids = list(active_ids)

    def active_ids(self):
        return self._active_ids


class _StubCtx:
    def __init__(self, data_dir, active_ids=()):
        self._data_dir = data_dir
        self.task_svc = _StubTaskSvc(active_ids)


def setup_function(_fn):
    drift_worker.reset_for_tests()
    project_activity._last_seen.clear()
    project_activity._last_request_at = 0.0


def _mark_seen_then_go_idle(monkeypatch, project: str) -> None:
    """mark_seen() ALSO counts as "the API was just busy" (correctly --
    marking a project seen literally means a request just landed), so a
    test that wants "recently used, but nothing pending RIGHT NOW" must
    let the idle gate clear first. Shrinks IDLE_GATE_S for the sleep so
    tests stay fast rather than waiting out the real default."""
    monkeypatch.setattr(drift_worker, "IDLE_GATE_S", 0.05)
    project_activity.mark_seen(project)
    time.sleep(0.08)


def _patch_common(monkeypatch, tmp_path, projects, repo_paths, active_tasks=None):
    active_tasks = active_tasks or {}

    def fake_get_all_projects():
        return list(projects)

    def fake_get_project(pid):
        d = tmp_path / "data" / pid
        d.mkdir(parents=True, exist_ok=True)
        return _StubCtx(d, active_ids=active_tasks.get(pid, ()))

    def fake_source_path(pid):
        return repo_paths.get(pid, "")

    import prism_service.project_context as pc
    import prism_service.services.claude_transcripts as ct
    import prism_service.engines.brain_engine as be

    monkeypatch.setattr(pc, "get_all_projects", fake_get_all_projects)
    monkeypatch.setattr(pc, "get_project", fake_get_project)
    monkeypatch.setattr(ct, "_project_source_path", fake_source_path)
    monkeypatch.setattr(be, "Brain", _StubBrain)


def test_a_project_nobody_has_opened_is_never_reindexed(tmp_path, monkeypatch):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _patch_common(monkeypatch, tmp_path,
                   projects=["neverseen"], repo_paths={"neverseen": str(repo)})

    results = drift_worker.sweep_once()

    assert results == [], (
        f"a project with NO recent client request and NO in-progress task "
        f"must never be reindexed -- this is the exact 'sweep everything "
        f"on every restart' defect being fixed; got {results!r}"
    )


def test_a_recently_seen_project_is_reindexed(tmp_path, monkeypatch):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _patch_common(monkeypatch, tmp_path,
                   projects=["prism"], repo_paths={"prism": str(repo)})
    _mark_seen_then_go_idle(monkeypatch, "prism")

    results = drift_worker.sweep_once()

    assert len(results) == 1 and results[0]["project"] == "prism", (
        f"a project seen via a real client request within the active "
        f"window must be reindexed this pass; got {results!r}")


def test_a_project_with_an_in_progress_task_is_reindexed_even_if_unseen(
        tmp_path, monkeypatch):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _patch_common(monkeypatch, tmp_path,
                   projects=["driving"], repo_paths={"driving": str(repo)},
                   active_tasks={"driving": ["task-1"]})

    results = drift_worker.sweep_once()

    assert len(results) == 1 and results[0]["project"] == "driving", (
        f"a project with a task actively in motion counts as in-use even "
        f"with no direct API traffic; got {results!r}")


def test_a_pending_client_request_gates_the_whole_sweep(tmp_path, monkeypatch):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _patch_common(monkeypatch, tmp_path,
                   projects=["prism"], repo_paths={"prism": str(repo)})
    project_activity.mark_seen("prism")  # in-use...
    project_activity.mark_seen("prism")  # ...and a request just landed

    results = drift_worker.sweep_once()

    assert results == [], (
        f"sweep_once() must never run WHILE the API is busy -- a request "
        f"landing within IDLE_GATE_S must block the entire pass, even for "
        f"an otherwise-qualifying project; got {results!r}")


def test_a_project_with_no_resolvable_root_is_dropped_and_never_reindexed(
        tmp_path, monkeypatch, capsys):
    _patch_common(monkeypatch, tmp_path,
                   projects=["ghost"], repo_paths={"ghost": ""})
    _mark_seen_then_go_idle(monkeypatch, "ghost")

    first = drift_worker.sweep_once()
    assert first == [], f"a project with no resolvable root must never reindex; got {first!r}"
    assert "ghost" in drift_worker._dropped

    out = capsys.readouterr().err
    assert "dropped from tracking" in out, f"expected a one-line drop log; got {out!r}"

    # A second sweep (still "seen") must not even attempt to resolve the
    # path again -- it's dropped, permanently, for this process lifetime.
    _mark_seen_then_go_idle(monkeypatch, "ghost")
    second = drift_worker.sweep_once()
    assert second == [], f"a dropped project must stay dropped; got {second!r}"


def test_budget_exhaustion_resumes_leftover_projects_on_the_next_call(
        tmp_path, monkeypatch):
    projects = [f"p{i}" for i in range(5)]
    repos = {}
    for p in projects:
        r = tmp_path / p
        r.mkdir()
        repos[p] = str(r)
    _patch_common(monkeypatch, tmp_path, projects=projects, repo_paths=repos)
    monkeypatch.setattr(drift_worker, "IDLE_GATE_S", 0.05)
    for p in projects:
        project_activity.mark_seen(p)
    time.sleep(0.08)

    # A budget of 0 means "no project may start" on the first call -- every
    # candidate must be queued for the NEXT call instead of skipped
    # outright (never silently dropped just because the budget was tight).
    monkeypatch.setattr(drift_worker, "BUDGET_S", 0.0)
    first = drift_worker.sweep_once()
    assert first == [], f"a zero budget must start no new project; got {first!r}"
    assert set(drift_worker._pending) == set(projects), (
        f"every candidate must be queued to resume on the next call; "
        f"got pending={drift_worker._pending!r}")

    # A generous budget on the NEXT call must drain the resumed queue.
    monkeypatch.setattr(drift_worker, "BUDGET_S", 30.0)
    time.sleep(0.08)
    second = drift_worker.sweep_once()
    assert {r["project"] for r in second} == set(projects), (
        f"the resumed queue must be drained once the budget allows it; "
        f"got {second!r}")
