"""The ship seat — the owner's Approve is what LANDS the branch (task 5b6aefc1).

Owner directive 2026-08-18: "I can approve it with visual evidence when I am
working it — not when I approve it you ship it. Make sure that approve SHIPS
it using the bot workflows, since it's always up to date locally."

The behavior this replaces inverted that: the shipped-ness tooth
(conductor_service `_unshipped_gate_reason`) refused the owner's approve until
the task's `[task:<id8>]` commits were already reachable from origin/main,
forcing a manual push/PR/merge BEFORE the click. Measured on the live board:
73 recorded refusals on task f506ece4, 16 on b835f639 — and both understate
the clicks, because the park helper only writes a row when the reason CHANGES.

REFINEMENT (owner, same day): "the merge is handled programmatically by the
bot workflows, not with tokens, unless there is an error, then that." So
everything below is plain Python + git + gh. This module imports no model
runner and spends no tokens; the LLM's job begins only after a stage has
failed and a human or a driving agent reads the parked reason.

SHIPS OFF BY DEFAULT, mirroring the two existing opt-in seats
(task_runner.py, gate_adjudicator.py): an environment opts in with
PRISM_SHIP_ON_APPROVE=1, and PRISM_SHIP_ON_APPROVE_INTERVAL=<seconds>
overrides the sweep cadence. With the var unset this module allocates no
thread and costs nothing.

WHAT THE SEAT MAY AND MAY NOT DO. It lands code; it never decides a gate.
The gate that finally passes is re-decided under the OWNER'S OWN actor
identity (the approval this seat recovers from history), so the conductor's
distinct-actor rule is untouched and `conductor-shipper` can never approve
its own work. Its own history rows are ship-stage audit rows only.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

SEAT_ID = "conductor-shipper"  # registered in actor_service.MACHINE_SEATS

SHIP_ENV = "PRISM_SHIP_ON_APPROVE"
INTERVAL_ENV = "PRISM_SHIP_ON_APPROVE_INTERVAL"
DEFAULT_INTERVAL_S = 0  # OFF unless an environment explicitly opts in
OPTED_IN_INTERVAL_S = 30  # cadence once opted in, unless INTERVAL_ENV overrides

# CI can legitimately take minutes; the wait is bounded so a stuck check parks
# with a reason instead of pinning a thread forever.
CI_TIMEOUT_S = 900.0
CI_POLL_S = 15.0

# (argv, cwd) -> (returncode, stdout, stderr). The ONE boundary tests stub;
# shaped after github_cli_auth.Runner, which already proved the seam.
Runner = Callable[..., tuple]

# `gh pr checks` exits 8 while checks are still pending, 0 when they all
# passed, and non-zero otherwise. Documented gh behavior, relied on here so
# the poll needs no JSON parsing.
GH_CHECKS_PENDING_RC = 8

_PR_NUM_RE = re.compile(r"/pull/(\d+)")


def _default_runner(argv: list, cwd: Optional[str] = None) -> tuple:
    proc = subprocess.run([str(a) for a in argv],
                          cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _log(msg: str) -> None:
    print(f"[ship-worker] {msg}", file=sys.stderr, flush=True)


def _interval_s() -> int:
    """Cadence in seconds; 0 (the default) means this environment is OUT."""
    raw = os.environ.get(INTERVAL_ENV, "")
    if raw.strip():
        try:
            return int(raw)
        except ValueError:
            return DEFAULT_INTERVAL_S
    opt = os.environ.get(SHIP_ENV, "").strip().lower()
    if opt in ("1", "true", "yes", "on"):
        return OPTED_IN_INTERVAL_S
    return DEFAULT_INTERVAL_S


def is_enabled() -> bool:
    """True when this environment opted into the seat. Consulted by the
    gate_decide trigger and the readiness disclosure too, so OFF means OFF:
    neither the sweep nor the approve-time hand-off runs."""
    return _interval_s() > 0


def _fail(stage: str, error: str, pr: Optional[int] = None) -> dict:
    return {"ok": False, "stage": stage, "error": str(error).strip(), "pr": pr}


def _run(runner: Runner, argv: list, cwd: Optional[str] = None) -> tuple:
    """Invoke the boundary, turning a MISSING BINARY into a readable reason.

    A daemon environment without `gh` is a real deployment state (the plan's
    explicit requirement): it must park the gate with an actionable line, not
    surface a FileNotFoundError traceback out of a background thread."""
    try:
        return runner(argv, cwd)
    except (FileNotFoundError, OSError) as exc:
        name = str(argv[0]) if argv else "?"
        return 127, "", (
            f"{name} is not available to the PRISM daemon ({exc}). Install it "
            f"and authenticate (`{name} auth login`) on the machine running "
            "the daemon, then approve again to retry.")
    except subprocess.SubprocessError as exc:
        return 1, "", f"{argv[0]} failed to run: {exc}"


def _workspace(task_id: str) -> tuple:
    from prism_service.services import task_workspace
    rec = task_workspace.workspace_for(task_id) or {}
    path = str(rec.get("path") or "")
    branch = str(rec.get("branch") or f"prism/ws/{task_id}")
    return path, branch


_REPO_SLUG_RE = re.compile(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$")


def _repo_slug(run: Runner, path: str) -> Optional[str]:
    """owner/repo for the ORIGIN remote, pinned explicitly on every `gh`
    call. Without this, `gh` auto-detects the repo from ALL configured
    remotes and prefers one named `upstream` when both `origin` and
    `upstream` point at GitHub — a fork's read-only/archived parent silently
    wins over the actually-pushable `origin`, and `gh pr create` fails with
    'Repository was archived so is read-only' even though the push to
    origin just succeeded. Pin it to origin's own remote, never let `gh`
    guess (observed live: task 356ffdd2 parked here 2026-08-23)."""
    try:
        rc, out, _err = run(["git", "remote", "get-url", "origin"], path)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if rc != 0:
        return None
    m = _REPO_SLUG_RE.search((out or "").strip())
    return m.group(1) if m else None


def _services(project: str, task_svc, cond):
    """Resolve the task/conductor services, tolerating their absence.

    ship_task is unit-testable against a bare git fixture with no task row at
    all, so a missing project context degrades to "pipeline only, no record
    and no replay" rather than raising."""
    if task_svc is not None and cond is not None:
        return task_svc, cond
    try:
        from prism_service.project_context import get_project
        ctx = get_project(project)
        return (task_svc or ctx.task_svc,
                cond or getattr(ctx, "conductor_svc", None))
    except Exception:
        return task_svc, cond


def _park(task_svc, task_id: str, stage: str, error: str) -> None:
    """Surface the EXACT stage error on the task and audit it under this seat.

    Parks PENDING, never `failed`: "evidence not ready" is a verdict on the
    ship, not on the work, and writing `failed` would force the owner's next
    honest approve through override=true (task 97d92854). The recorded
    approval is deliberately left in place so the next sweep retries.
    A computed-then-dropped refusal is the e0149f1f defect class this repo
    already named — the reason goes on the row, always."""
    if task_svc is None:
        return
    reason = (f"green_gate: ship failed at {stage} — {error} "
              "(your approval is recorded and will be retried; "
              f"seat={SEAT_ID})")
    try:
        task_svc.update(task_id, gate_state="pending", gate_reason=reason,
                        blocked_reason=reason)
    except Exception:
        pass
    try:
        task_svc.record_history(
            task_id, action="ship",
            details=f"stage={stage}; result=failed; error={error}",
            actor=SEAT_ID)
    except Exception:
        pass


def _audit(task_svc, task_id: str, stage: str, detail: str = "") -> None:
    if task_svc is None:
        return
    try:
        task_svc.record_history(
            task_id, action="ship",
            details=f"stage={stage}; result=ok{('; ' + detail) if detail else ''}",
            actor=SEAT_ID)
    except Exception:
        pass


def ship_task(task_id: str, project: str = "default", *,
              runner: Optional[Runner] = None,
              task_svc=None, cond=None,
              poll_interval_s: float = CI_POLL_S,
              ci_timeout_s: float = CI_TIMEOUT_S,
              on_landed: Optional[Callable[..., bool]] = None) -> dict:
    """Land this task's workspace branch. DETERMINISTIC — no model calls.

    push -> gh pr create (body carries the `[task:<id8>]` trailer, which is
    what `api/tasks.py::_is_shipped_on_main` greps for on origin/main) ->
    gh pr checks polled to completion -> gh pr merge -> fetch, so the local
    origin/main ref reflects the landing the shipped-ness tooth is about to
    re-read.

    ``on_landed(task_svc, cond, task_id) -> bool`` runs once the branch is
    on origin/main — the pluggable "what does landing mean for this task"
    step. Defaults to ``_replay_owner_approval`` (the human-approved,
    ship-on-approve track); ``sweep_once`` passes ``_adjudicate_after_ship``
    for the machine-adjudicated track instead. Neither seat approves
    anything beyond what its OWN authority already grants — this only
    removes the one objection (unshipped-ness) that shipping just cleared.

    Returns {"ok", "stage", "error", "pr"}. On failure the gate is parked with
    the verbatim stage error and the owner's recorded approval is preserved.
    """
    run = runner or _default_runner
    task_svc, cond = _services(project, task_svc, cond)
    short = task_id[:8]

    path, branch = _workspace(task_id)
    if not path or not os.path.isdir(path):
        res = _fail("resolve_workspace",
                    f"no workspace checkout resolves for task {short} "
                    f"(looked for {path or '<unrecorded>'})")
        _park(task_svc, task_id, res["stage"], res["error"])
        return res

    # ---- push --------------------------------------------------------------
    rc, out, err = _run(run, ["git", "push", "origin", branch], path)
    if rc != 0:
        res = _fail("push", err or out or f"git push exited {rc}")
        _park(task_svc, task_id, res["stage"], res["error"])
        return res
    _audit(task_svc, task_id, "push", f"branch={branch}")

    # Pin every `gh` call to origin's own repo — `gh` otherwise auto-detects
    # from ALL configured remotes and can silently resolve to a read-only
    # `upstream` fork instead of the `origin` this branch was just pushed to.
    slug = _repo_slug(run, path)
    repo_flag = ["--repo", slug] if slug else []

    # ---- open the PR -------------------------------------------------------
    title = f"ship: {short} — landed from the green_gate approve"
    body = (f"Landed by the {SEAT_ID} seat on the owner's green_gate approve.\n"
            f"\n[task:{short}]\n")
    rc, out, err = _run(run, [
        "gh", "pr", "create", "--head", branch, "--base", "main",
        "--title", title, "--body", body, *repo_flag], path)
    if rc != 0:
        already_exists = "already exists" in (err or out or "").lower()
        m = _PR_NUM_RE.search(err or "") if already_exists else None
        if not m:
            res = _fail("pr_create", err or out or f"gh pr create exited {rc}")
            _park(task_svc, task_id, res["stage"], res["error"])
            return res
        pr = int(m.group(1))
    else:
        m = _PR_NUM_RE.search(out or "")
        pr = int(m.group(1)) if m else 0
    _audit(task_svc, task_id, "pr_create", f"pr={pr}")

    # ---- wait for CI -------------------------------------------------------
    deadline = time.monotonic() + float(ci_timeout_s)
    while True:
        rc, out, err = _run(run, ["gh", "pr", "checks", str(pr), *repo_flag], path)
        if rc == 0:
            break
        if rc != GH_CHECKS_PENDING_RC:
            res = _fail("ci_wait", err or out or f"gh pr checks exited {rc}", pr)
            _park(task_svc, task_id, res["stage"], res["error"])
            return res
        if time.monotonic() >= deadline:
            res = _fail("ci_wait",
                        f"CI was still pending after {int(ci_timeout_s)}s", pr)
            _park(task_svc, task_id, res["stage"], res["error"])
            return res
        if poll_interval_s:
            time.sleep(float(poll_interval_s))
    _audit(task_svc, task_id, "ci_wait", f"pr={pr}")

    # ---- merge -------------------------------------------------------------
    rc, out, err = _run(run, [
        "gh", "pr", "merge", str(pr), "--squash", "--delete-branch",
        *repo_flag], path)
    if rc != 0:
        res = _fail("merge", err or out or f"gh pr merge exited {rc}", pr)
        _park(task_svc, task_id, res["stage"], res["error"])
        return res
    _audit(task_svc, task_id, "merge", f"pr={pr}")

    # Refresh origin/main locally: the shipped-ness tooth reads THIS checkout's
    # origin/main ref, so without the fetch it would re-refuse what just landed.
    _run(run, ["git", "fetch", "origin"], path)

    land = on_landed or _replay_owner_approval
    replayed = land(task_svc, cond, task_id)
    return {"ok": True, "stage": "merged", "error": "", "pr": pr,
            "replayed": replayed}


def _replay_owner_approval(task_svc, cond, task_id: str) -> bool:
    """Re-present the OWNER'S OWN preserved decision now that it can succeed.

    This is the line the seat must not cross: it re-decides the gate as the
    HUMAN who approved, never as itself. `conductor-shipper` landing code and
    then approving its own landing would be exactly the self-approval the
    distinct-actor rule exists to prevent."""
    if cond is None or task_svc is None:
        return False
    try:
        approval = cond._recorded_ship_approval(task_id)
    except Exception:
        approval = None
    if not approval:
        return False
    try:
        cond.gate_decide(task_id, "approve",
                         reason=str(approval.get("reason") or ""),
                         actor=str(approval.get("actor") or ""),
                         session_id=SEAT_ID)
        return True
    except Exception as exc:
        _park(task_svc, task_id, "replay", f"{type(exc).__name__}: {exc}")
        return False


def _adjudicate_after_ship(task_svc, cond, task_id: str) -> bool:
    """The MACHINE-TRACK twin of _replay_owner_approval, for a proof_type=
    test task whose green_gate was blocked SOLELY on shipped-ness — no
    human approval was ever recorded because none is required for this
    track (the adjudicator seat, task eaafdf75, already owns its green).

    Missing final step this closes (owner 2026-08-21): a fully-autonomous
    task could clear every OTHER green_gate tooth and then park forever,
    because nothing ever pushed/PR'd/merged its branch — `_awaiting_ship`
    only ever found tasks carrying a recorded HUMAN 'ship=queued' approval,
    which a machine-adjudicated task never has. Re-running
    `adjudicate_green_gate` here grants NO new authority: it re-checks every
    pre-flight tooth (candidate-controls-judge, reachability, screen-claim,
    the fresh-receipt requirement) from scratch and only approves if all of
    them — now including shipped-ness — are clean."""
    if cond is None or task_svc is None:
        return False
    try:
        res = cond.adjudicate_green_gate(task_id)
        return bool(res and res.get("ok"))
    except Exception as exc:
        _park(task_svc, task_id, "replay", f"{type(exc).__name__}: {exc}")
        return False


def _awaiting_ship(project: str) -> list:
    """Task ids whose green_gate carries a recorded, unshipped HUMAN
    approval (the ship-on-approve, proof_type=demo/review track)."""
    from prism_service.project_context import get_project
    ctx = get_project(project)
    cond = getattr(ctx, "conductor_svc", None)
    if cond is None:
        return []
    out = []
    for task in ctx.task_svc.list(status="in_progress"):
        tid = str(getattr(task, "id", "") or "")
        if str(getattr(task, "workflow_step", "")) != "green_gate":
            continue
        try:
            if not cond._recorded_ship_approval(tid):
                continue
            if not cond._unshipped_gate_reason(task):
                continue
        except Exception:
            continue
        out.append(tid)
    return out


MAX_SHIP_ATTEMPTS_PER_SWEEP = 3
# Bounds blast radius per tick, mirroring task_runner's own per-tick caps
# (_max_turns, _max_load_per_core, etc.): a pass with several simultaneously
# eligible tasks makes real progress on more than one of them (including
# skipping past a stuck task) without unbounded gh/network traffic if the
# whole board suddenly becomes eligible at once. The next sweep picks up
# wherever this one left off, so 3 is a modest per-tick budget, not a hard
# ceiling on throughput.
STALL_THRESHOLD = 3
# 3 consecutive IDENTICAL (stage, error) failures for the SAME task is
# enough to rule out a one-off transient flake -- a genuine flake usually
# clears, or at least changes its error text, within a retry or two -- while
# not being so low that a single bad poll permanently blacklists a task that
# would have shipped on a normal retry.

_FAILURE_STREAKS: dict[str, dict] = {}
# In-memory, per-process counter of consecutive identical (stage, error)
# ship failures, keyed by task_id. Cleared on any success and on daemon
# restart. A restart resetting this is fine: it is a fresh start for the
# circuit breaker, and a GENUINELY stuck task (a real merge conflict, a
# permanently broken CI check) simply re-accumulates identical failures
# across the next few sweeps and gets re-blocked -- the same root cause
# keeps producing the same stage/error, so it does not quietly retry
# forever just because a restart cleared the counter.


def _note_ship_result(pid: str, task_id: str, res: dict) -> None:
    """Update task_id's consecutive-identical-failure streak; once it hits
    STALL_THRESHOLD, flip the task to `blocked` with a real reason so
    sweep_once stops retrying it (task_svc.list(status="in_progress") — what
    _awaiting_ship/_awaiting_ship_machine both scan — naturally excludes a
    blocked task from the next sweep) and a human or another workflow can
    see and act on the real problem.

    A failure whose stage OR error text differs from the running streak
    resets the count to 1 rather than blocking early — see AC(c): only a
    task that fails the SAME way repeatedly is stuck; one that fails
    differently each time is still making distinguishable attempts.
    """
    if res.get("ok"):
        _FAILURE_STREAKS.pop(task_id, None)
        return

    stage = str(res.get("stage") or "")
    error = str(res.get("error") or "")
    prev = _FAILURE_STREAKS.get(task_id)
    if prev and prev.get("stage") == stage and prev.get("error") == error:
        count = int(prev.get("count", 0)) + 1
    else:
        count = 1
    _FAILURE_STREAKS[task_id] = {"stage": stage, "error": error, "count": count}

    if count < STALL_THRESHOLD:
        return

    _FAILURE_STREAKS.pop(task_id, None)
    task_svc, _cond = _services(pid, None, None)
    reason = (
        f"ship_worker: stuck at {stage} for {STALL_THRESHOLD} consecutive "
        f"identical attempts -- {error} (needs a manual fix -- e.g. "
        "resolving a merge conflict or fixing a broken CI check -- before "
        "ship_worker will retry it automatically)")
    if task_svc is not None:
        try:
            task_svc.update(task_id, status="blocked", blocked_reason=reason)
        except Exception:
            pass
        try:
            task_svc.record_history(
                task_id, action="ship",
                details=f"stage={stage}; result=blocked; error={error}",
                actor=SEAT_ID)
        except Exception:
            pass
    _log(f"{task_id[:8]}: blocked after {STALL_THRESHOLD} identical "
         f"failures at {stage} -- {error}")


def _awaiting_ship_machine(project: str) -> list:
    """Task ids on the MACHINE-adjudicated track (proof_type NOT demo/review)
    parked at a pending green_gate for shipped-ness — no human approval
    involved or required, so `_awaiting_ship`'s recorded-approval filter
    never finds them. This is the gap: a fully-autonomous task can clear
    every other green_gate tooth and then park forever, since nothing else
    ever pushes/PRs/merges its branch."""
    from prism_service.project_context import get_project
    ctx = get_project(project)
    cond = getattr(ctx, "conductor_svc", None)
    if cond is None:
        return []
    out = []
    for task in ctx.task_svc.list(status="in_progress"):
        tid = str(getattr(task, "id", "") or "")
        if str(getattr(task, "workflow_step", "")) != "green_gate":
            continue
        if str(getattr(task, "gate_state", "")) != "pending":
            continue
        pt = str(getattr(task, "proof_type", "") or "").strip().lower()
        if pt in ("demo", "review"):
            continue  # human-only track (owner rule eaafdf75); _awaiting_ship owns it
        try:
            if not cond._unshipped_gate_reason(task):
                continue
        except Exception:
            continue
        out.append(tid)
    return out


def sweep_once() -> Optional[dict]:
    """One pass over every project: attempt up to MAX_SHIP_ATTEMPTS_PER_SWEEP
    eligible tasks, bounding blast radius the way task_runner bounds its own
    per-tick work. Checks the human-approved track first (an explicit owner
    decision waiting to land), then the machine track (nothing is waiting on
    a person).

    Previously this returned on the FIRST attempt regardless of outcome, so
    one persistently failing task (a genuinely conflicting PR) occupied the
    single per-pass slot forever and starved every OTHER eligible task —
    observed live: task 0e2c82f3's merge-conflicted PR #2348 starved the
    healthy, already-green_gate-passed task 8b4e7cb6 for 20+ minutes. Now a
    failed attempt moves on to the next eligible task within the same pass
    (bounded by MAX_SHIP_ATTEMPTS_PER_SWEEP), and a task that keeps failing
    the same way is blocked by _note_ship_result so it stops consuming the
    queue's attention entirely.

    Returns the result of the LAST ship attempt made this pass (or None if
    nothing was eligible) — the same single-result shape callers/tests
    already rely on.
    """
    from prism_service.project_context import get_all_projects

    attempts = 0
    last: Optional[dict] = None

    for pid in get_all_projects():
        try:
            pending = _awaiting_ship(pid)
        except Exception as exc:
            _log(f"{pid}: eligibility check failed: {exc}")
            pending = []
        for tid in pending:
            if attempts >= MAX_SHIP_ATTEMPTS_PER_SWEEP:
                return last
            res = ship_task(tid, pid)
            attempts += 1
            last = res
            _log(f"{pid}/{tid[:8]}: {res}")
            _note_ship_result(pid, tid, res)

        try:
            pending_machine = _awaiting_ship_machine(pid)
        except Exception as exc:
            _log(f"{pid}: machine-track eligibility check failed: {exc}")
            pending_machine = []
        for tid in pending_machine:
            if attempts >= MAX_SHIP_ATTEMPTS_PER_SWEEP:
                return last
            res = ship_task(tid, pid, on_landed=_adjudicate_after_ship)
            attempts += 1
            last = res
            _log(f"{pid}/{tid[:8]}: {res}")
            _note_ship_result(pid, tid, res)

    return last


def _loop(interval_s: int) -> None:
    _log(f"started; interval={interval_s}s")
    while True:
        try:
            sweep_once()
        except Exception as exc:
            _log(f"sweep error: {exc}")
        time.sleep(interval_s)


def start_ship_worker() -> Optional[threading.Thread]:
    """Spawn the ship daemon thread, unless this environment did not opt in
    (the default). Mirrors start_task_runner / start_gate_adjudicator: same
    shape, same off-by-default posture."""
    interval = _interval_s()
    if interval <= 0:
        _log(f"disabled (default OFF; set {SHIP_ENV}=1 to opt this "
             "environment in)")
        return None
    t = threading.Thread(target=_loop, args=(interval,),
                         name="prism-ship-worker", daemon=True)
    t.start()
    return t
