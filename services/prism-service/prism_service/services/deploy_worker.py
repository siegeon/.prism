"""Deploy seat -- a shipped build reaches the dev instance without a hand.

Task 13cfe8ee. Owner 2026-08-27: "a lot of this flow should have been part
of the conductor flow in the app, not up to you." ship_worker lands a
task's branch on origin/main; nothing after that deploys it, so a session
did the same four steps by hand: git pull in the shared checkout, a status
check, npm run build of web_dist, an aspire restart, then poll /api/version.

THE TRIGGER IS A SUCCESSFUL LAND, same as reap/brain-health/refresh-maps
(see ship_worker._reap_after_land's docstring). `deploy_after_land` is the
hook ship_worker calls once a branch is on origin/main; `/api/deploy/run`
is the manual trigger -- both call this module's own `deploy_once`, so a
hand click and an automatic land can never disagree about what "deploy"
does (the same "one implementation, two entry points" shape brain-health
and refresh-maps already use).

A LAND `deploy_after_land` NEVER SEES is still covered: that hook only
fires from ship_worker.ship_task, so a branch that reaches origin/main by
a direct push (this repo's own self-dev carve-out) or any other route
never calls it. `sweep_new_land` is the standing sweep thread's OWN tick
(`_tick`, run before `sweep_pending` on every interval): it fetches, and
whenever the checkout's upstream is ahead of a clean HEAD it runs the
identical `deploy_once` pipeline with no task in the loop -- a bare land
observed only via git has no task to attribute evidence to, but the
outcome is always logged, never silently skipped.

THE RESTART PRIMITIVE. `auto_updater.perform_restart()` (main-thread
os.execv, issue #66's guard) is reached only through
`auto_updater.request_restart()` -- a daemon-thread flag the uvicorn main
thread polls and acts on (main.py's `_restart_watcher`). This seat calls
exactly that same primitive; it never execs anything itself, and its own
call to `deploy_once` does not survive the restart it just asked for --
that is WHY the pipeline splits into a `requested` half (this call) and a
`confirmed`/`parked` half (`confirm_pending_deploy`, run on the FRESH
process's next tick, reading the durable history row the dying process
left behind rather than any in-memory state).

REFUSAL, not silent skip: a dirty POLICY_FILES entry (or any other
uncommitted change) in the daemon's own checkout means the running judge
cannot prove it executes the code it claims to -- the same tooth a driving
session was told to apply BY HAND before ever bouncing the daemon
(CLAUDE.md: "BEFORE BOUNCING THE DEV DAEMON, run git status ... and REFUSE
if any POLICY_FILES entry is dirty"). The deploy parks with that reason
and touches nothing else.

OFF BY DEFAULT (task_runner.py / gate_adjudicator.py / ship_worker.py
posture): PRISM_DEPLOY_ON_LAND=1 opts an environment in. PRISM_DEPLOY_COMMAND,
when set, REPLACES the pull/build/restart-request steps with one shell
command -- an environment whose deploy is a single script (rsync, an
ansible play, a CD pipeline trigger) is not forced through this module's
own opinions about git pull + npm build.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from prism_service.services import system_activity

SEAT_ID = "conductor-deployer"  # registered in actor_service.MACHINE_SEATS

DEPLOY_ENV = "PRISM_DEPLOY_ON_LAND"
COMMAND_ENV = "PRISM_DEPLOY_COMMAND"
SWEEP_INTERVAL_ENV = "PRISM_DEPLOY_SWEEP_INTERVAL"
DEFAULT_SWEEP_INTERVAL_S = 30

RUN_TIMEOUT_S = 900  # npm run build can take a while on a cold cache

VERSION_POLL_INTERVAL_S = 5.0
VERSION_POLL_TIMEOUT_S = 180.0

_VERSION_REL = "services/prism-service/prism_service/__version__.py"
_WEB_REL_PREFIX = "services/prism-service/prism_service/web/"

_DEPLOY_ACTION = "deploy"
_STAGE_REQUESTED = "requested"
_STAGE_CONFIRMED = "confirmed"
_STAGE_PARKED = "parked"
_STAGE_SKIPPED = "skipped"

# (argv, cwd) -> (returncode, stdout, stderr). The ONE boundary tests stub,
# same shape as ship_worker.Runner.
Runner = Callable[..., tuple]


def _default_runner(argv, cwd=None) -> tuple:
    cmd = [str(a) for a in argv]
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True,
                              timeout=RUN_TIMEOUT_S)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 1, "", f"{cmd[0] if cmd else '?'} timed out after {RUN_TIMEOUT_S}s"
    except (FileNotFoundError, OSError) as exc:
        name = cmd[0] if cmd else "?"
        return 127, "", f"{name} is not available to the PRISM daemon ({exc})"


def _log(msg: str) -> None:
    print(f"[deploy-worker] {msg}", file=sys.stderr, flush=True)


def is_enabled() -> bool:
    """True when this environment opted into deploy-on-land. OFF by
    default, mirroring task_runner/gate_adjudicator/ship_worker's own
    posture."""
    return os.environ.get(DEPLOY_ENV, "").strip().lower() in (
        "1", "true", "yes", "on")


def _deploy_command() -> str:
    """Non-empty when the operator wants THIS shell command to replace the
    built-in pull+build+restart-request steps entirely."""
    return os.environ.get(COMMAND_ENV, "").strip()


def _repo_root() -> Optional[Path]:
    """The daemon's own checkout -- the same ascend-to-`.git` resolution
    control_plane.dirty_judge_reason already trusts."""
    from prism_service.services.control_plane import _prism_repo_root
    return _prism_repo_root()


def dirty_policy_reason(run: Runner, repo_root: Path) -> str:
    """Non-empty when a POLICY_FILES entry has uncommitted changes in
    `repo_root`. Runs through the injected `run` so a test never touches
    the real daemon checkout -- unlike
    control_plane.dirty_policy_files_in_daemon_checkout, which always
    shells out for real against `_prism_repo_root()`."""
    from prism_service.services.control_plane import POLICY_FILES, is_policy_file

    rc, out, _err = run(
        ["git", "status", "--porcelain", "--", *POLICY_FILES], repo_root)
    if rc != 0 or not (out or "").strip():
        return ""
    changed = set()
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if is_policy_file(path):
            changed.add(path)
    if not changed:
        return ""
    return ("dirty policy file(s) in the checkout: " + ", ".join(sorted(changed)) +
           " -- commit or discard them before deploying; the running judge "
           "must prove it is executing the code it claims to")


def dirty_checkout_reason(run: Runner, repo_root: Path) -> str:
    """Non-empty when the daemon's own checkout is not clean: either a
    POLICY_FILES entry is dirty (the security tooth `dirty_policy_reason`
    applies), or the tree has ANY other uncommitted change -- a pull/build
    must never run against uncommitted local edits it did not ask for."""
    policy = dirty_policy_reason(run, repo_root)
    if policy:
        return policy
    rc, out, _err = run(["git", "status", "--short"], repo_root)
    if rc == 0 and (out or "").strip():
        return ("uncommitted changes in the checkout -- commit or discard "
               "them before deploying")
    return ""


def _fail(stage: str, error: str) -> dict:
    return {"ok": False, "stage": stage, "error": str(error).strip()}


def _rev_parse(run: Runner, repo_root: Path, rev: str = "HEAD") -> str:
    rc, out, _err = run(["git", "rev-parse", rev], repo_root)
    return out.strip() if rc == 0 else ""


def _pull_ff_only(run: Runner, repo_root: Path) -> dict:
    rc, out, err = run(["git", "fetch", "origin"], repo_root)
    if rc != 0:
        return _fail("fetch", err or out or f"git fetch exited {rc}")
    rc, out, err = run(["git", "pull", "--ff-only"], repo_root)
    if rc != 0:
        return _fail("pull", err or out or f"git pull exited {rc}")
    return {"ok": True}


def _web_changed(run: Runner, repo_root: Path, before_sha: str) -> bool:
    """True when the fast-forward moved past any web-relevant file (a TSX/
    TS/CSS file, or anything under the web package) -- a rebuild is wasted
    work otherwise. Fails OPEN (assumes changed) on any diff error or an
    unresolvable `before_sha`, so an unresolvable comparison never silently
    skips a real rebuild."""
    if not before_sha:
        return True
    rc, out, _err = run(
        ["git", "diff", "--name-only", before_sha, "HEAD"], repo_root)
    if rc != 0:
        return True
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(_WEB_REL_PREFIX) or line.endswith((".tsx", ".ts", ".css")):
            return True
    return False


def _build_web(run: Runner, repo_root: Path) -> dict:
    web_dir = repo_root / "services" / "prism-service" / "prism_service" / "web"
    rc, out, err = run(["npm", "run", "build"], web_dir)
    if rc != 0:
        return _fail("build_web", err or out or f"npm run build exited {rc}")
    return {"ok": True}


def _run_custom_command(run: Runner, repo_root: Path, command: str) -> dict:
    rc, out, err = run(["sh", "-c", command], repo_root)
    if rc != 0:
        return _fail("deploy_command", err or out or f"{command!r} exited {rc}")
    return {"ok": True}


def _read_version(repo_root: Path) -> str:
    """The version literal now on disk at HEAD -- read directly off the
    filesystem rather than imported, so this never reports the RUNNING
    process's stale, already-loaded module."""
    path = repo_root / _VERSION_REL
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'PRISM_VERSION\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def _default_request_restart() -> None:
    from prism_service.services import auto_updater
    auto_updater.request_restart()


def _record(task_svc, task_id: str, detail: str) -> None:
    """Best-effort audit row under this seat's own identity, same shape as
    ship_worker._audit/_park. Never raises: a broken recorder must never
    break the deploy itself."""
    if task_svc is None or not task_id:
        return
    try:
        task_svc.record_history(
            task_id, action=_DEPLOY_ACTION, details=detail, actor=SEAT_ID)
    except Exception:
        pass


def deploy_once(*, repo_root: Optional[Path] = None,
                runner: Optional[Runner] = None,
                request_restart: Optional[Callable[[], None]] = None,
                task_svc=None, task_id: str = "",
                project: str = "default") -> dict:
    """Run the deploy pipeline ONCE: refuse on a dirty checkout, then either
    the operator's own PRISM_DEPLOY_COMMAND or fetch+ff-pull+(conditional)
    web rebuild, then request the running daemon's own in-place restart
    (auto_updater.request_restart -- issue #66's safe handoff, never an
    exec from this thread).

    Records a `requested` history row naming the target version so a FRESH
    process (after the real restart) can confirm it later via
    `confirm_pending_deploy` -- this call's own thread does not survive the
    restart it just asked for. Deterministic, no model calls, same posture
    as ship_worker.ship_task: `runner` and `request_restart` are the two
    injectable boundaries a test replaces, and a custom PRISM_DEPLOY_COMMAND
    is trusted to perform its OWN restart -- it is never followed by a call
    to `request_restart`.
    """
    run = runner or _default_runner
    root = repo_root if repo_root is not None else _repo_root()
    if root is None or not root.is_dir():
        res = _fail("resolve_checkout",
                    "no PRISM checkout resolves for the deploy seat")
        _record(task_svc, task_id, f"stage={_STAGE_PARKED}; error={res['error']}")
        return res

    reason = dirty_checkout_reason(run, root)
    if reason:
        res = _fail("dirty_checkout", reason)
        _record(task_svc, task_id, f"stage={_STAGE_PARKED}; error={reason}")
        return res

    before_sha = _rev_parse(run, root)
    command = _deploy_command()

    if command:
        step_res = _run_custom_command(run, root, command)
        if not step_res.get("ok"):
            _record(task_svc, task_id,
                   f"stage={_STAGE_PARKED}; error={step_res.get('error', '')}")
            return step_res
        target_version = _read_version(root)
        _record(task_svc, task_id,
               f"stage={_STAGE_REQUESTED}; target_version={target_version}")
        return {"ok": True, "stage": _STAGE_REQUESTED,
               "target_version": target_version, "via": "custom_command"}

    pull_res = _pull_ff_only(run, root)
    if not pull_res.get("ok"):
        _record(task_svc, task_id,
               f"stage={_STAGE_PARKED}; error={pull_res.get('error', '')}")
        return pull_res

    if _web_changed(run, root, before_sha):
        build_res = _build_web(run, root)
        if not build_res.get("ok"):
            _record(task_svc, task_id,
                   f"stage={_STAGE_PARKED}; error={build_res.get('error', '')}")
            return build_res

    target_version = _read_version(root)
    restart = request_restart or _default_request_restart
    try:
        restart()
    except Exception as exc:
        res = _fail("restart", f"{type(exc).__name__}: {exc}")
        _record(task_svc, task_id, f"stage={_STAGE_PARKED}; error={res['error']}")
        return res

    _record(task_svc, task_id,
           f"stage={_STAGE_REQUESTED}; target_version={target_version}")
    return {"ok": True, "stage": _STAGE_REQUESTED,
           "target_version": target_version, "via": "pull_build_restart"}


def _pending_deploy_target(task_svc, task_id: str) -> Optional[str]:
    """The target version of the most recent UNRESOLVED deploy request for
    this task -- the last `deploy` history row being `requested` is what
    marks it pending; a later confirmed/parked/skipped row means it already
    resolved. None when there is nothing pending."""
    if task_svc is None or not task_id:
        return None
    try:
        rows = task_svc.history(task_id)
    except Exception:
        return None
    for row in reversed(rows or []):
        if str(getattr(row, "action", "")) != _DEPLOY_ACTION:
            continue
        details = str(getattr(row, "details", "") or "")
        if details.startswith(f"stage={_STAGE_REQUESTED}"):
            m = re.search(r"target_version=(\S+)", details)
            target = (m.group(1) if m else "").strip()
            return target or None
        return None  # most recent deploy row already resolved
    return None


def _fetch_version(api_base: str, timeout: float = 5.0) -> Optional[str]:
    """GET {api_base}/api/version -> its `.version` string, or None on any
    failure (unreachable mid-restart, non-200, bad JSON)."""
    try:
        req = urllib.request.Request(f"{api_base.rstrip('/')}/api/version")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        v = str(data.get("version") or "").strip()
        return v or None
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        return None


def confirm_pending_deploy(*, task_svc=None, task_id: str = "",
                          api_base: str = "", version_fetcher=None,
                          poll_interval_s: float = VERSION_POLL_INTERVAL_S,
                          timeout_s: float = VERSION_POLL_TIMEOUT_S) -> dict:
    """The seat on its next tick: confirm or park a deploy a PREVIOUS
    process (before its own restart) already requested. Polls /api/version
    (or the injected `version_fetcher`) for up to `timeout_s` in
    `poll_interval_s` steps, comparing against the target this task's own
    `requested` history row named. A match writes `confirmed` with the
    observed version as the task's deploy evidence; a timeout writes
    `parked` with the mismatch reason. A task with no pending request is a
    no-op ({"ok": True, "stage": "skipped"})."""
    target = _pending_deploy_target(task_svc, task_id)
    if not target:
        return {"ok": True, "stage": _STAGE_SKIPPED,
               "reason": "no pending deploy request for this task"}

    fetch = version_fetcher or (
        lambda: _fetch_version(api_base or "http://127.0.0.1:7780"))
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    seen = ""
    while True:
        seen = fetch() or ""
        if seen == target:
            _record(task_svc, task_id, f"stage={_STAGE_CONFIRMED}; version={seen}")
            try:
                if task_svc is not None and task_id:
                    task_svc.update(task_id,
                                    completion_proof=f"deployed version {seen}")
            except Exception:
                pass
            return {"ok": True, "stage": _STAGE_CONFIRMED, "version": seen}
        if time.monotonic() >= deadline:
            why = (f"target={target} not observed after {int(timeout_s)}s "
                  f"(last seen: {seen or '<unreachable>'})")
            _record(task_svc, task_id, f"stage={_STAGE_PARKED}; error={why}")
            return _fail("version_mismatch", why)
        if poll_interval_s:
            time.sleep(float(poll_interval_s))


def deploy_after_land(task_svc, task_id: str, project: str = "default") -> None:
    """THE TRIGGER IS A SUCCESSFUL LAND (ship_worker.ship_task, same
    trigger as reap/brain-health/refresh-maps -- see
    ship_worker._reap_after_land's docstring). Best-effort: never re-runs
    or blocks a ship that already succeeded, and does nothing at all unless
    this environment opted in (`is_enabled`)."""
    if not is_enabled():
        return
    try:
        deploy_once(task_svc=task_svc, task_id=task_id, project=project)
    except Exception:  # noqa: BLE001 - a deploy attempt never fails a ship
        pass


def _upstream_ahead_count(run: Runner, repo_root: Path) -> int:
    """Commits the checkout's upstream (`@{u}`) is ahead of HEAD, after
    fetching origin for real. -1 on any git failure -- no upstream
    configured, a detached HEAD, an unreachable remote -- so an ambiguous
    comparison never triggers a deploy attempt."""
    rc, _out, _err = run(["git", "fetch", "origin"], repo_root)
    if rc != 0:
        return -1
    rc, out, _err = run(["git", "rev-list", "--count", "HEAD..@{u}"], repo_root)
    if rc != 0:
        return -1
    try:
        return int((out or "").strip())
    except ValueError:
        return -1


def sweep_new_land(*, repo_root: Optional[Path] = None,
                   runner: Optional[Runner] = None,
                   request_restart: Optional[Callable[[], None]] = None) -> dict:
    """THE GAP `deploy_after_land` never covers: that hook only fires from
    ship_worker.ship_task, so a branch that reaches origin/main by a DIRECT
    push (this repo's own self-dev carve-out, see CLAUDE.md) or by any
    route other than ship_task never deploys at all -- the standing sweep
    thread used to only CONFIRM an already-`requested` deploy
    (`sweep_pending`), never START one. This is the seat's own tick: a
    dirty checkout still parks with a reason, checked BEFORE any fetch (the
    same security tooth `deploy_once` itself applies -- a checkout that
    cannot prove it runs the code it claims to is never compared against
    the remote at all); a clean checkout whose upstream is ahead of HEAD
    gets the identical `deploy_once` pull+build+restart pipeline a
    task-scoped land would have gotten. No task_id -- a bare land observed
    only via git has no task to attribute evidence to -- but every non-ok
    outcome is still logged (never a silent skip) so a user can see why
    nothing happened."""
    run = runner or _default_runner
    root = repo_root if repo_root is not None else _repo_root()
    if root is None or not root.is_dir():
        return {"ok": True, "stage": _STAGE_SKIPPED,
               "reason": "no PRISM checkout resolves for the deploy seat"}

    reason = dirty_checkout_reason(run, root)
    if reason:
        _log(f"sweep: parked, {reason}")
        return _fail("dirty_checkout", reason)

    ahead = _upstream_ahead_count(run, root)
    if ahead <= 0:
        return {"ok": True, "stage": _STAGE_SKIPPED,
               "reason": "upstream not ahead of HEAD"}

    _log(f"sweep: upstream is {ahead} commit(s) ahead of HEAD; deploying")
    result = deploy_once(repo_root=root, runner=run, request_restart=request_restart)
    if not result.get("ok"):
        _log(f"sweep: deploy failed at stage={result.get('stage')}: "
             f"{result.get('error')}")
    return result


def sweep_pending() -> None:
    """Confirm every task with a still-pending deploy request, across every
    project -- the durable, cross-restart half of the pipeline. A fresh
    process after a real restart has none of the previous process's
    in-memory state, only what `deploy_once` wrote to task history before
    it asked to be restarted, so this is what actually resolves it."""
    from prism_service.project_context import get_all_projects, get_project

    for pid in get_all_projects():
        try:
            ctx = get_project(pid)
        except Exception:
            continue
        task_svc = getattr(ctx, "task_svc", None)
        if task_svc is None:
            continue
        try:
            candidates = list(task_svc.list(status="done"))
        except Exception:
            candidates = []
        for task in candidates:
            tid = str(getattr(task, "id", "") or "")
            if tid and _pending_deploy_target(task_svc, tid):
                try:
                    confirm_pending_deploy(task_svc=task_svc, task_id=tid)
                except Exception:
                    pass


def _tick() -> None:
    """One iteration of the sweep loop, split out of `_loop` so a test can
    pin the order without running an infinite loop. Starts a deploy for a
    land the sweep has not seen yet BEFORE confirming anything a previous
    tick (or a task-scoped land) already requested -- either half's own
    exception never stops the other, same as the loop's own posture."""
    with system_activity.pass_("deploy_sweep", "*", "sweep_new_land+sweep_pending"):
        try:
            sweep_new_land()
        except Exception as exc:
            _log(f"sweep new-land error: {exc}")
        try:
            sweep_pending()
        except Exception as exc:
            _log(f"sweep error: {exc}")


def _loop(interval_s: int) -> None:
    _log(f"started; interval={interval_s}s")
    while True:
        _tick()
        time.sleep(interval_s)


def start_deploy_worker() -> Optional[threading.Thread]:
    """Spawn the deploy sweep thread, unless this environment did not opt
    in (the default). Mirrors start_ship_worker/start_task_runner: same
    shape, same off-by-default posture. Each tick (`_tick`) both STARTS a
    deploy for a land the seat has not seen yet (`sweep_new_land` -- the
    gap left by any land that never went through ship_worker's post-land
    hook or the /api/deploy/run trigger) and CONFIRMS a still-pending
    deploy request (`sweep_pending`, including after a restart this same
    module requested)."""
    if not is_enabled():
        _log(f"disabled (default OFF; set {DEPLOY_ENV}=1 to opt this "
             "environment in)")
        return None
    try:
        interval = int(os.environ.get(SWEEP_INTERVAL_ENV, "") or
                       DEFAULT_SWEEP_INTERVAL_S)
    except ValueError:
        interval = DEFAULT_SWEEP_INTERVAL_S
    t = threading.Thread(target=_loop, args=(interval,),
                         name="prism-deploy-worker", daemon=True)
    t.start()
    return t
