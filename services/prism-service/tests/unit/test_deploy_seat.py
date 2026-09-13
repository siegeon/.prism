"""The deploy seat: a shipped build reaches the dev instance without a hand
-- task 13cfe8ee.

Owner 2026-08-27: "a lot of this flow should have been part of the
conductor flow in the app, not up to you." After ship_worker lands a task
on origin/main, nothing deployed it -- a session did the same four steps
by hand (git pull, status check, npm run build, an aspire restart, poll
/api/version) four times in one day.

  AC(a)  a dirty POLICY_FILES entry in the checkout PARKS the deploy with a
         reason, and touches nothing else (no fetch/pull/npm/restart call).
  AC(b)  the happy path: fetch + ff-pull, a conditional web rebuild (only
         when the pulled range touched web-relevant files), then exactly
         one call to the injected restart primitive -- never an exec of
         its own. Records the shipped target version.
  AC(c)  PRISM_DEPLOY_COMMAND, when set, REPLACES pull/build/restart with
         one shell command, and never calls the restart primitive itself.
  AC(d)  confirm_pending_deploy resolves a version match to `confirmed`
         (with the observed version written as evidence), a mismatch past
         its deadline to `parked`, and a task with no pending request to a
         no-op `skipped`.
  AC(e)  "conductor-deployer" is a REGISTERED machine seat.
  AC(f)  default OFF -- deploy_after_land does nothing unless
         PRISM_DEPLOY_ON_LAND opts the environment in.

This suite never touches the real daemon checkout and never restarts
anything: git runs for REAL against throwaway repos this file builds, but
`npm`/the restart primitive/the version fetcher are all fakes passed in
through deploy_worker's own injectable boundaries.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import deploy_worker  # noqa: E402

TASK_ID = "13cfe8ee-7ded-4f32-921d-e4bad1aa5165"
_GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

_VERSION_REL = deploy_worker._VERSION_REL
_WEB_TSX_REL = deploy_worker._WEB_REL_PREFIX + "src/App.tsx"
_POLICY_REL = "services/prism-service/prism_service/services/conductor_service.py"


def _git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          env=_GIT_ENV, capture_output=True,
                          text=True).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _version_text(version: str) -> str:
    return f'PRISM_VERSION = "{version}"\nPRISM_VERSION_NOTES = ""\n'


def _make_repo(tmp_path: Path):
    """A real bare `origin` + a `work` checkout on `main`, one commit in,
    at version 1.0.0 -- the state deploy_once operates on."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _write(work, _VERSION_REL, _version_text("1.0.0"))
    _write(work, "README.md", "# baseline\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "baseline")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-q", "-u", "origin", "main")
    return origin, work


def _land_from_elsewhere(tmp_path: Path, origin: Path, *, version: str,
                         touch_web: bool) -> None:
    """Simulate ship_worker landing a task's branch: a SECOND clone commits
    a version bump (+ optionally a web file) and pushes to origin/main,
    putting `work` one fast-forward behind."""
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    # `origin`'s HEAD symref was set at `init --bare` time, before `main`
    # ever existed there, so it still names an unborn ref (commonly
    # "master") that a plain clone cannot check out -- CI runners default
    # `init.defaultBranch` to that builtin fallback, landing `other` on
    # the wrong (unborn) local branch and turning the push below into
    # "src refspec main does not match any" (reproduced on a fresh
    # ubuntu:24.04 container; masked on a machine whose global
    # `init.defaultBranch` happens to already be "main"). `origin/main`
    # was fetched during the clone regardless, so check it out explicitly.
    _git(other, "checkout", "-q", "main")
    _write(other, _VERSION_REL, _version_text(version))
    if touch_web:
        _write(other, _WEB_TSX_REL, "export const App = () => null;\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", f"chore(version): {version}")
    _git(other, "push", "-q", "origin", "main")


class FakeRunner:
    """Runs `git` FOR REAL (so a fast-forward genuinely changes the files
    on disk that `_read_version`/`_web_changed` then read); fakes `npm` and
    any custom shell command so the suite never shells out to a real
    Node toolchain."""

    def __init__(self, *, npm_rc: int = 0, npm_err: str = "",
                 custom_rc: int = 0, custom_err: str = ""):
        self.calls: list[tuple] = []
        self.npm_rc = npm_rc
        self.npm_err = npm_err
        self.custom_rc = custom_rc
        self.custom_err = custom_err

    def __call__(self, argv, cwd=None):
        self.calls.append((list(str(a) for a in argv), str(cwd) if cwd else None))
        head = str(argv[0])
        if head == "git":
            proc = subprocess.run([str(a) for a in argv],
                                  cwd=str(cwd) if cwd else None, env=_GIT_ENV,
                                  capture_output=True, text=True)
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        if head == "npm":
            return self.npm_rc, "", self.npm_err
        if head == "sh":
            return self.custom_rc, "", self.custom_err
        return 127, "", f"unexpected command {argv!r}"


class _Row:
    def __init__(self, action: str, details: str):
        self.action = action
        self.details = details


class FakeTaskSvc:
    """The one boundary confirm_pending_deploy/deploy_once read and write --
    an in-memory stand-in for TaskService, never a real project/daemon."""

    def __init__(self):
        self._history: dict[str, list] = {}
        self.updates: dict[str, dict] = {}

    def record_history(self, task_id, action="", details="", actor=""):
        self._history.setdefault(task_id, []).append(_Row(action, details))

    def history(self, task_id):
        return list(self._history.get(task_id, []))

    def update(self, task_id, **kwargs):
        self.updates.setdefault(task_id, {}).update(kwargs)


def test_dirty_policy_file_parks_and_touches_nothing_else(tmp_path):
    """AC(a): a POLICY_FILES entry with uncommitted changes parks the
    deploy -- and the seat never even attempts a fetch, pull, npm build, or
    restart once it sees that."""
    _origin, work = _make_repo(tmp_path)
    _write(work, _POLICY_REL, "# an uncommitted edit\n")

    run = FakeRunner()
    restarted = []
    task_svc = FakeTaskSvc()

    result = deploy_worker.deploy_once(
        repo_root=work, runner=run, request_restart=lambda: restarted.append(1),
        task_svc=task_svc, task_id=TASK_ID)

    assert result["ok"] is False
    assert result["stage"] == "dirty_checkout"
    assert _POLICY_REL in result["error"]
    assert not restarted
    assert not any(c[0][0] == "npm" for c in run.calls)
    assert not any(c[0][:2] == ["git", "fetch"] for c in run.calls)
    rows = task_svc.history(TASK_ID)
    assert rows and rows[-1].action == "deploy"
    assert rows[-1].details.startswith("stage=parked")


def test_happy_path_rebuilds_web_and_requests_restart_once(tmp_path):
    """AC(b): a landed commit that touches a web file gets pulled AND
    rebuilt, then the restart primitive is called exactly once (never an
    exec of its own), and the requested target version is what's now on
    disk after the pull."""
    origin, work = _make_repo(tmp_path)
    _land_from_elsewhere(tmp_path, origin, version="1.1.0", touch_web=True)

    run = FakeRunner()
    restarted = []
    task_svc = FakeTaskSvc()

    result = deploy_worker.deploy_once(
        repo_root=work, runner=run, request_restart=lambda: restarted.append(1),
        task_svc=task_svc, task_id=TASK_ID)

    assert result == {"ok": True, "stage": "requested",
                      "target_version": "1.1.0", "via": "pull_build_restart"}
    assert restarted == [1]
    assert any(c[0][:2] == ["git", "fetch"] for c in run.calls)
    assert any(c[0][:2] == ["git", "pull"] for c in run.calls)
    assert any(c[0][0] == "npm" for c in run.calls)
    rows = task_svc.history(TASK_ID)
    assert rows[-1].details == "stage=requested; target_version=1.1.0"


def test_non_web_landing_skips_the_rebuild(tmp_path):
    """The other half of AC(b): a landed commit that never touches web
    source is still pulled and still restarts, but never wastes an npm
    build on it."""
    origin, work = _make_repo(tmp_path)
    _land_from_elsewhere(tmp_path, origin, version="1.2.0", touch_web=False)

    run = FakeRunner()
    restarted = []

    result = deploy_worker.deploy_once(
        repo_root=work, runner=run, request_restart=lambda: restarted.append(1))

    assert result["ok"] is True
    assert result["target_version"] == "1.2.0"
    assert restarted == [1]
    assert not any(c[0][0] == "npm" for c in run.calls)


def test_deploy_command_replaces_pull_build_restart(tmp_path, monkeypatch):
    """AC(c): PRISM_DEPLOY_COMMAND runs instead of pull+build, and the
    built-in restart primitive is NEVER called -- the custom command is
    trusted to perform its own."""
    _origin, work = _make_repo(tmp_path)
    monkeypatch.setenv(deploy_worker.COMMAND_ENV, "echo redeploying")

    run = FakeRunner()
    restarted = []
    task_svc = FakeTaskSvc()

    result = deploy_worker.deploy_once(
        repo_root=work, runner=run, request_restart=lambda: restarted.append(1),
        task_svc=task_svc, task_id=TASK_ID)

    assert result["ok"] is True
    assert result["via"] == "custom_command"
    assert result["target_version"] == "1.0.0"
    assert not restarted
    assert any(c[0][:2] == ["sh", "-c"] for c in run.calls)
    assert not any(c[0][:2] == ["git", "fetch"] for c in run.calls)
    assert not any(c[0][0] == "npm" for c in run.calls)


def test_confirm_pending_deploy_matches_target_and_records_evidence():
    """AC(d), the match half: a `requested` row plus a version_fetcher that
    now reports the same target resolves to `confirmed`, with the observed
    version written both to history and as the task's completion_proof
    evidence."""
    task_svc = FakeTaskSvc()
    task_svc.record_history(TASK_ID, action="deploy",
                            details="stage=requested; target_version=7.13.315")

    result = deploy_worker.confirm_pending_deploy(
        task_svc=task_svc, task_id=TASK_ID, version_fetcher=lambda: "7.13.315")

    assert result == {"ok": True, "stage": "confirmed", "version": "7.13.315"}
    rows = task_svc.history(TASK_ID)
    assert rows[-1].details == "stage=confirmed; version=7.13.315"
    assert task_svc.updates[TASK_ID]["completion_proof"] == \
        "deployed version 7.13.315"


def test_confirm_pending_deploy_parks_on_timeout_mismatch():
    """AC(d), the mismatch half: the served version never matches before
    the deadline -> parked, never confirmed."""
    task_svc = FakeTaskSvc()
    task_svc.record_history(TASK_ID, action="deploy",
                            details="stage=requested; target_version=7.13.315")

    result = deploy_worker.confirm_pending_deploy(
        task_svc=task_svc, task_id=TASK_ID, version_fetcher=lambda: "7.13.312",
        poll_interval_s=0, timeout_s=0)

    assert result["ok"] is False
    assert result["stage"] == "version_mismatch"
    assert "7.13.315" in result["error"]
    rows = task_svc.history(TASK_ID)
    assert rows[-1].details.startswith("stage=parked")


def test_confirm_pending_deploy_is_a_noop_with_nothing_pending():
    """AC(d), the no-op half: a task whose last deploy row already resolved
    (or that has none at all) is `skipped`, never re-polled."""
    task_svc = FakeTaskSvc()

    result = deploy_worker.confirm_pending_deploy(
        task_svc=task_svc, task_id=TASK_ID, version_fetcher=lambda: "anything")

    assert result == {"ok": True, "stage": "skipped",
                      "reason": "no pending deploy request for this task"}


def test_disabled_by_default_and_deploy_after_land_is_a_noop(monkeypatch):
    """AC(f): with PRISM_DEPLOY_ON_LAND unset, is_enabled() is False and the
    post-land hook never calls deploy_once at all."""
    monkeypatch.delenv(deploy_worker.DEPLOY_ENV, raising=False)
    assert deploy_worker.is_enabled() is False

    called = []
    monkeypatch.setattr(deploy_worker, "deploy_once",
                        lambda **kw: called.append(kw) or {"ok": True})
    deploy_worker.deploy_after_land(None, TASK_ID)
    assert not called


def test_opted_in_deploy_after_land_calls_deploy_once(monkeypatch):
    """The other half of AC(f): PRISM_DEPLOY_ON_LAND=1 makes the post-land
    hook actually run the pipeline."""
    monkeypatch.setenv(deploy_worker.DEPLOY_ENV, "1")
    assert deploy_worker.is_enabled() is True

    called = []
    monkeypatch.setattr(deploy_worker, "deploy_once",
                        lambda **kw: called.append(kw) or {"ok": True})
    deploy_worker.deploy_after_land("svc", TASK_ID, project="prism")
    assert len(called) == 1
    assert called[0]["task_id"] == TASK_ID
    assert called[0]["project"] == "prism"


def test_sweep_new_land_deploys_a_bare_land_with_no_task(tmp_path):
    """THE GAP: ship_worker's post-land hook only fires from ship_task, so
    a branch that reaches origin/main by a DIRECT push (this repo's own
    self-dev carve-out; see CLAUDE.md) or any other route never calls
    deploy_after_land -- nothing else would ever notice the land. This is
    the seat's own tick: no task in the loop, it still fetches, sees the
    upstream ahead of HEAD, pulls, and requests exactly one restart."""
    origin, work = _make_repo(tmp_path)
    _land_from_elsewhere(tmp_path, origin, version="1.3.0", touch_web=False)

    run = FakeRunner()
    restarted = []

    result = deploy_worker.sweep_new_land(
        repo_root=work, runner=run, request_restart=lambda: restarted.append(1))

    assert result["ok"] is True
    assert result["target_version"] == "1.3.0"
    assert restarted == [1]
    assert any(c[0][:2] == ["git", "rev-list"] for c in run.calls)


def test_sweep_new_land_is_a_noop_when_head_already_matches_upstream(tmp_path):
    """No new land -> the upstream-ahead count is 0 -> skipped, and the
    restart primitive (and npm) are never touched -- this must not restart
    the daemon every single tick."""
    _origin, work = _make_repo(tmp_path)

    run = FakeRunner()
    restarted = []

    result = deploy_worker.sweep_new_land(
        repo_root=work, runner=run, request_restart=lambda: restarted.append(1))

    assert result == {"ok": True, "stage": "skipped",
                      "reason": "upstream not ahead of HEAD"}
    assert not restarted
    assert not any(c[0][0] == "npm" for c in run.calls)


def test_sweep_new_land_parks_on_dirty_checkout_without_fetching(tmp_path):
    """The same security tooth deploy_once applies: a dirty POLICY_FILES
    entry parks BEFORE any fetch, so a checkout that cannot prove it is
    running the code it claims to is never even compared against the
    remote."""
    _origin, work = _make_repo(tmp_path)
    _write(work, _POLICY_REL, "# an uncommitted edit\n")

    run = FakeRunner()
    restarted = []

    result = deploy_worker.sweep_new_land(
        repo_root=work, runner=run, request_restart=lambda: restarted.append(1))

    assert result["ok"] is False
    assert result["stage"] == "dirty_checkout"
    assert _POLICY_REL in result["error"]
    assert not restarted
    assert not any(c[0][:2] == ["git", "fetch"] for c in run.calls)


def test_tick_starts_new_land_before_confirming_pending(monkeypatch):
    """The sweep thread's own loop body: on every tick it must check for a
    land the sweep hasn't seen yet BEFORE it confirms anything already
    pending -- split out of the infinite `_loop` so this is pinned without
    running a real loop or sleeping."""
    order = []
    monkeypatch.setattr(deploy_worker, "sweep_new_land",
                        lambda: order.append("new_land"))
    monkeypatch.setattr(deploy_worker, "sweep_pending",
                        lambda: order.append("pending"))

    deploy_worker._tick()

    assert order == ["new_land", "pending"]


def test_sweep_new_land_skips_the_fetch_when_the_cache_is_warm(tmp_path):
    """COST tooth (owner brief, 2026-09-13): a fetch is real network+process
    work, paid every _tick (default 30s). Two sweeps of the SAME clean repo
    inside the cache TTL, with nothing signalled, must issue exactly ONE
    `git fetch` between them -- the second sweep answers from the cached
    ahead-count instead of shelling out again."""
    _origin, work = _make_repo(tmp_path)
    deploy_worker.reset_fetch_cache()
    run = FakeRunner()

    first = deploy_worker.sweep_new_land(
        repo_root=work, runner=run, request_restart=lambda: None)
    fetches_after_first = sum(1 for c in run.calls if c[0][:2] == ["git", "fetch"])
    second = deploy_worker.sweep_new_land(
        repo_root=work, runner=run, request_restart=lambda: None)
    fetches_after_second = sum(1 for c in run.calls if c[0][:2] == ["git", "fetch"])

    assert first["stage"] == "skipped"
    assert second["stage"] == "skipped"
    assert fetches_after_first == 1
    assert fetches_after_second == 1  # the second sweep fetched nothing new


def test_upstream_ahead_count_only_refetches_when_signalled_or_stale(tmp_path):
    """The caching primitive itself, isolated from the rest of the deploy
    pipeline (whose OWN pull step does a second, unrelated fetch once a
    deploy actually proceeds -- not what this tooth is about). A warm
    cache answers a second call with ZERO fetches; `signal_land` forces
    exactly one more, after which the cache is warm again."""
    _origin, work = _make_repo(tmp_path)
    deploy_worker.reset_fetch_cache()
    run = FakeRunner()

    first = deploy_worker._upstream_ahead_count(run, work)
    fetches_after_first = sum(1 for c in run.calls if c[0][:2] == ["git", "fetch"])
    assert first == 0
    assert fetches_after_first == 1

    second = deploy_worker._upstream_ahead_count(run, work)
    fetches_after_second = sum(1 for c in run.calls if c[0][:2] == ["git", "fetch"])
    assert second == 0
    assert fetches_after_second == fetches_after_first  # answered from cache

    deploy_worker.signal_land(work)
    third = deploy_worker._upstream_ahead_count(run, work)
    fetches_after_third = sum(1 for c in run.calls if c[0][:2] == ["git", "fetch"])
    assert third == 0
    assert fetches_after_third == fetches_after_second + 1  # forced, exactly once

    fourth = deploy_worker._upstream_ahead_count(run, work)
    fetches_after_fourth = sum(1 for c in run.calls if c[0][:2] == ["git", "fetch"])
    assert fetches_after_fourth == fetches_after_third  # warm again, no signal left


def test_sweep_new_land_notices_a_signalled_land_inside_a_warm_cache(tmp_path):
    """End to end: a warm cache must never hide a REAL land from the sweep
    when that land was signalled -- the escape hatch this seat needs since
    a direct push (this repo's own self-dev carve-out) has no other way to
    invalidate the cache early."""
    origin, work = _make_repo(tmp_path)
    deploy_worker.reset_fetch_cache()
    run = FakeRunner()

    deploy_worker.sweep_new_land(
        repo_root=work, runner=run, request_restart=lambda: None)  # warms the cache

    _land_from_elsewhere(tmp_path, origin, version="1.3.0", touch_web=False)
    deploy_worker.signal_land(work)
    restarted = []
    result = deploy_worker.sweep_new_land(
        repo_root=work, runner=run, request_restart=lambda: restarted.append(1))

    assert result["ok"] is True
    assert result["target_version"] == "1.3.0"
    assert restarted == [1]


def test_sweep_pending_confirms_only_ids_with_a_real_pending_request(
        tmp_path, monkeypatch):
    """200ms-bar cost tooth (owner brief, 2026-09-13): sweep_pending used to
    list EVERY done task and call history() on each just to find the rare
    one with a pending deploy request. It must now confirm exactly the ids
    TaskService.pending_deploy_task_ids names -- zero confirm attempts for
    an ordinary done task with no deploy history at all."""
    from prism_service.services.task_service import TaskService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    plain_done = task_svc.create(title="ordinary done task")
    task_svc.update(plain_done.id, status="done")
    pending = task_svc.create(title="task with a pending deploy")
    task_svc.update(pending.id, status="done")
    task_svc.record_history(
        pending.id, action="deploy", details="stage=requested; target_version=1.2.3")

    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.task_svc = task_svc
    monkeypatch.setattr(
        "prism_service.project_context.get_all_projects", lambda: ["proj"])
    monkeypatch.setattr(
        "prism_service.project_context.get_project", lambda pid: ctx)

    confirmed = []
    monkeypatch.setattr(deploy_worker, "confirm_pending_deploy",
                        lambda *, task_svc, task_id: confirmed.append(task_id))

    deploy_worker.sweep_pending()

    assert confirmed == [pending.id], (
        "only the task with a real pending deploy row may be confirmed -- "
        "the ordinary done task must cost zero confirm attempts")


def test_deployer_seat_is_a_registered_machine_seat():
    """AC(e): an unregistered actor writing history resolves to
    ActorKind.UNKNOWN -- the audited defect ship_worker's own suite already
    pins for "conductor-shipper"; this seat must be registered the same
    way."""
    from prism_service.services import actor_service

    assert deploy_worker.SEAT_ID == "conductor-deployer"
    assert deploy_worker.SEAT_ID in actor_service.MACHINE_SEATS


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
