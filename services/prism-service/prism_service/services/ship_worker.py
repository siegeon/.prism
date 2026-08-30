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

import ast
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

# Task 811fcce0: GitHub has not always registered the PR's checks by the
# very FIRST poll right after push+pr_create -- `gh pr checks` then exits
# non-zero (not GH_CHECKS_PENDING_RC) with stderr reading exactly "no
# checks reported on the '<base>' branch". That is a false negative, not
# a real check failure: no check has been scheduled against this branch
# yet, not "a check ran and failed". A short, SEPARATE grace window
# (bounded, distinct from CI_TIMEOUT_S's own pending-deadline) covers
# exactly this one string; any OTHER ci_wait failure (a check that ran
# and genuinely failed) still fails on its first poll, unchanged -- a
# blanket retry across every failure would mask a real one behind an
# unnecessary wait, which is the ticket's own named likely_misfire.
CI_NOT_YET_REGISTERED_GRACE_S = 60.0
_NO_CHECKS_REGISTERED_RE = re.compile(
    r"no checks reported on the '[^']*' branch", re.IGNORECASE)

# Ceiling on ONE git/gh call. On timeout the WHOLE process group dies, not
# only the direct child: `git push` over HTTPS spawns `git-remote-https`
# beneath it, and `subprocess.run(timeout=)` left that helper alive with its
# github.com connection open (two orphans observed live 2026-08-26).
RUN_TIMEOUT_S = 120

# (argv, cwd) -> (returncode, stdout, stderr). The ONE boundary tests stub;
# shaped after github_cli_auth.Runner, which already proved the seam.
Runner = Callable[..., tuple]

# `gh pr checks` exits 8 while checks are still pending, 0 when they all
# passed, and non-zero otherwise. Documented gh behavior, relied on here so
# the poll needs no JSON parsing.
GH_CHECKS_PENDING_RC = 8

# task <ship-worker-pending-rc>: GH_CHECKS_PENDING_RC=8 is not what every gh
# CLI build actually returns for a normal pending check -- this machine's
# gh 2.4.0 (2022) returns rc=1 with stdout reading exactly
# "checks\tpending\t0\t<url>", so every pending poll fell straight into the
# "any OTHER ci_wait failure" branch and failed on its FIRST poll, never
# once entering the retry-wait loop. The tab-row's own STATUS column
# already says "pending"/"queued"/"in_progress" in every gh version this
# was checked against, so that text is trusted over a magic exit code that
# disagrees across gh builds.
_PENDING_STATUS_RE = re.compile(
    r"\b(pending|queued|in_progress|waiting)\b", re.IGNORECASE)

_PR_NUM_RE = re.compile(r"/pull/(\d+)")


def _default_runner(argv: list, cwd: Optional[str] = None) -> tuple:
    cmd = [str(a) for a in argv]
    group_kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if sys.platform.startswith("win") else {"start_new_session": True})
    proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, **group_kw)
    try:
        out, err = proc.communicate(timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        proc.communicate()
        raise
    return proc.returncode, out or "", err or ""


def _kill_process_group(proc: "subprocess.Popen") -> None:
    """Kill the child AND every descendant in its own group/session."""
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


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


_VERSION_REL = "services/prism-service/prism_service/__version__.py"
# Last reason the version-conflict resolver gave up, surfaced into the
# park message so a CI-only failure names itself instead of being guessed at.
_LAST_RESOLVE_ERROR: list = []


_MAX_VERSION_CONFLICT_PASSES = 20


def _resolve_version_only_conflict(run: Runner, path: str,
                                   conflicts: list[str],
                                   _depth: int = 0) -> bool:
    """Resolve a rebase conflict whose ONLY casualty is __version__.py.

    SUPERSEDES this module's original refusal (task 229954e4) to touch any
    conflict, "not even the common case of two branches both bumping
    PRISM_VERSION". That reasoning holds for real content -- a silent guess
    is worse than parking -- and it does NOT hold here, because this
    resolution is mechanical rather than a guess: every task bumps the same
    literal and appends its own changelog entry at the same place, so the
    two sides never disagree about meaning. Measured 2026-08-29: three
    tasks blocked at the ship rebase, __version__.py a casualty in two.

    The merge is: take THEIR file (origin/main, which already carries every
    entry that shipped), bump its patch by exactly one, and append this
    branch's own new entries. Nothing is invented, nothing is discarded,
    and two tasks can never claim the same version.

    Returns True only when it actually resolved; any doubt returns False
    and the caller parks exactly as before.
    """
    if _depth >= _MAX_VERSION_CONFLICT_PASSES:
        return False
    if [c.strip() for c in conflicts] != [_VERSION_REL]:
        return False
    import re
    full = os.path.join(path, _VERSION_REL)
    # STAGE ORDER, and it is inverted from the intuition: during a REBASE
    # git replays each commit ONTO the upstream, so stage 2 ("ours") is
    # origin/main -- the branch we are landing onto -- and stage 3
    # ("theirs") is the task's own commit being replayed. Reading them the
    # other way round takes the BRANCH's version literal as the base and
    # drops every changelog entry main has gained since the branch was cut.
    # That is not hypothetical: the first version of this resolver had them
    # swapped, and it rewrote two task branches to PRISM_VERSION 7.13.125
    # while main was at 7.13.156, deleting 115 changelog lines. Neither
    # shipped, but only because both were blocked for other reasons.
    rc_base, base, _e = _run(run, ["git", "show", f":2:{_VERSION_REL}"], path)
    rc_br, branch, _e2 = _run(run, ["git", "show", f":3:{_VERSION_REL}"], path)
    if rc_base != 0 or rc_br != 0 or not base.strip() or not branch.strip():
        return False
    m = re.search(r'PRISM_VERSION = "(\d+)\.(\d+)\.(\d+)"', base)
    if not m:
        return False
    maj, mi, pa = m.groups()
    # ONE BRANCH CLAIMS ONE VERSION. Only the first pass bumps; a branch
    # with several version-touching commits conflicts once per replayed
    # commit, and bumping on each would march the number forward by the
    # number of commits rather than by one, which is arbitrary and makes
    # the version say something about a branch's shape instead of about
    # release order. Later passes keep the number the first pass settled on
    # (it is already this file's literal by then).
    bumped = (f'PRISM_VERSION = "{maj}.{mi}.{int(pa) + 1}"' if _depth == 0
              else m.group(0))
    merged = base.replace(m.group(0), bumped, 1)
    # Append every changelog line the BRANCH added that main does not have.
    added = [ln for ln in branch.splitlines()
             if ln.strip().startswith('"\\n') and ln not in base.splitlines()]
    if added:
        idx = merged.rstrip().rfind(")")
        if idx == -1:
            return False
        merged = merged[:idx] + "\n".join(added) + "\n" + merged[idx:]
    try:
        ast.parse(merged)
    except SyntaxError:
        return False  # never hand a broken module to the next stage
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(merged)
    rc, _o, _e = _run(run, ["git", "add", _VERSION_REL], path)
    if rc != 0:
        return False
    # COMPLETING THE STEP ACROSS GIT VERSIONS. git 2.34 (this dev box) and
    # git 2.43 on ubuntu-24.04 (CI) disagree about which editor knob a
    # `rebase --continue` consults: the older apply backend honours
    # core.editor, the newer merge backend reaches for the commit-message
    # editor and can stop waiting for input that never comes. Set BOTH
    # knobs, and if the continue still refuses, commit the staged
    # resolution explicitly with --no-edit and then continue. Measured:
    # these two tests passed on 2.34 locally and failed on CI's newer git
    # for four runs, which is the same local-vs-CI git split already
    # recorded against task 3f46342a.
    # git 2.55 (CI) ignored the `-c core.editor` form that satisfied 2.34 and
    # 2.43, so the continue stalled and both portability tests went red on
    # main. GIT_EDITOR/GIT_SEQUENCE_EDITOR in the ENVIRONMENT is the knob
    # every version honours, and it outranks the config one, so set both:
    # `env` carries it without changing `_run`'s (runner, argv, cwd) shape.
    _ED = ["env", "GIT_EDITOR=true", "GIT_SEQUENCE_EDITOR=true"]
    # COMMITTER IDENTITY. Both `rebase --continue` and the explicit commit
    # below WRITE a commit, and a CI runner or a fresh container often has
    # no identity configured at all. Measured on CI 2026-08-29 (run
    # 33275400720), after the git 2.55 editor fix landed: "Committer
    # identity unknown ... fatal: empty ident name", which took both
    # portability tests red on main a second time. Supplied ONLY when git
    # has no identity of its own, so a real ship keeps its real committer
    # rather than being relabelled by the resolver.
    _id_rc, _id_out, _ = _run(run, ["git", "config", "user.email"], path)
    _ID = [] if (_id_rc == 0 and (_id_out or "").strip()) else [
        "-c", "user.email=ship-worker@prism.local",
        "-c", "user.name=prism-ship-worker"]
    cont = _ED + ["git"] + _ID + [
        "-c", "core.editor=true", "-c", "sequence.editor=true",
        "rebase", "--continue"]
    env_rc, _o2, _e2 = _run(run, cont, path)
    if env_rc != 0:
        # The staged file IS the resolution; make the commit without an
        # editor, then let the rebase carry on.
        c_rc, _o3, _e3 = _run(
            run, _ED + ["git"] + _ID + ["commit", "--no-edit"], path)
        if c_rc == 0:
            env_rc, _o2, _e2 = _run(run, cont, path)
    if env_rc == 0:
        return True
    # Could not reproduce locally (git 2.34) what CI's git fails on, so carry
    # git's OWN words out to the caller rather than guess a third time.
    _LAST_RESOLVE_ERROR.clear()
    _LAST_RESOLVE_ERROR.append(
        f"rebase --continue rc={env_rc}: {(_e2 or _o2 or '').strip()[:400]}")
    # A branch with SEVERAL commits touching the version file hits a fresh
    # conflict on each replayed commit. Resolving only the first leaves the
    # rebase stopped on the second, the caller aborts, and the task parks --
    # observed live on task afb47c33, where --continue walked 3/5, 4/5, 5/5
    # and stopped on another version conflict. Keep resolving while the
    # conflict set stays EXACTLY the version file; the moment anything else
    # appears, hand back False and let the caller park as before. The bound
    # is a safety net against a loop that never converges, not an expected
    # limit -- a branch with 20 version-touching commits is already odd.
    _rc, again, _e3 = _run(
        run, ["git", "diff", "--name-only", "--diff-filter=U"], path)
    remaining = [f for f in again.splitlines() if f.strip()]
    if not remaining:
        return False  # stopped for a reason we did not cause; caller parks
    return _resolve_version_only_conflict(run, path, remaining, _depth + 1)


def _rebase_onto_main(run: Runner, path: str) -> dict:
    """Fetch origin/main and rebase (or fast-forward) the task's own branch
    onto its current tip BEFORE push (task 229954e4). A branch cut hours
    earlier, based on a commit main has since moved past, gets pushed
    as-is otherwise -- GitHub cannot resolve mergeable/statusCheckRollup
    (both read UNKNOWN/CONFLICTING), and `gh pr checks` reports "no checks
    reported" forever. That is a stale-base symptom, not a real CI failure.

    Returns {"ok": True} on an already-current fast path (no `git rebase`
    call issued at all -- AC(c)), {"ok": True, "rebased": True} after a
    clean rebase, or {"ok": False, "stage", "error"} on a genuine conflict
    or any other rebase/fetch failure.

    NEVER auto-resolves a real content conflict -- not even the common case
    of two branches both bumping PRISM_VERSION on the same line. Per this
    task's own likely_misfire, a silent guess here is worse than parking:
    the rebase is aborted CLEANLY (the worktree is left exactly as it was,
    never mid-rebase) and the conflicting file(s) are named verbatim so the
    stall-guard's blocked_reason gives a human something to act on.
    """
    rc, out, err = _run(run, ["git", "fetch", "origin", "main"], path)
    if rc != 0:
        return {"ok": False, "stage": "fetch_main",
                "error": err or out or f"git fetch exited {rc}"}

    rc, _out, _err = _run(
        run, ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"], path)
    if rc == 0:
        return {"ok": True}  # already current -- no wasted rebase attempt

    rc, out, err = _run(run, ["git", "rebase", "origin/main"], path)
    if rc == 0:
        return {"ok": True, "rebased": True}

    # Genuine conflict (or another rebase failure): name it, then abort so
    # the worktree returns to its pre-rebase state -- never leave it
    # mid-rebase for the next sweep or a human to trip over.
    _rc, conflicts_out, _err = _run(
        run, ["git", "diff", "--name-only", "--diff-filter=U"], path)
    files = [f for f in conflicts_out.splitlines() if f.strip()]
    # Task 3161c0d5: a conflict whose ONLY casualty is the version file is
    # mechanical, not a judgement call -- resolve it and carry on. Every
    # other conflict set still parks, unchanged, per task 229954e4.
    if _resolve_version_only_conflict(run, path, files):
        return {"ok": True, "rebased": True, "version_conflict_resolved": True}
    _run(run, ["git", "rebase", "--abort"], path)
    detail = (f"conflicts in {', '.join(files)}" if files
             else (err or out or f"git rebase exited {rc}"))
    if _LAST_RESOLVE_ERROR:
        detail += f" [resolver: {_LAST_RESOLVE_ERROR[-1]}]"
    return {"ok": False, "stage": "rebase",
            "error": f"rebase onto origin/main {detail} -- needs manual "
                     "resolution"}


def ship_task(task_id: str, project: str = "default", *,
              runner: Optional[Runner] = None,
              task_svc=None, cond=None,
              poll_interval_s: float = CI_POLL_S,
              ci_timeout_s: float = CI_TIMEOUT_S,
              not_yet_registered_grace_s: float = CI_NOT_YET_REGISTERED_GRACE_S,
              on_landed: Optional[Callable[..., bool]] = None) -> dict:
    """Land this task's workspace branch. DETERMINISTIC — no model calls.

    rebase (or fast-forward) onto current origin/main -> push -> gh pr create
    (body carries the `[task:<id8>]` trailer, which is what
    `api/tasks.py::_is_shipped_on_main` greps for on origin/main) -> gh pr
    checks polled to completion -> gh pr merge -> fetch, so the local
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

    # ---- rebase onto current origin/main, before push -----------------------
    rebase_res = _rebase_onto_main(run, path)
    if not rebase_res.get("ok"):
        res = _fail(rebase_res["stage"], rebase_res["error"])
        _park(task_svc, task_id, res["stage"], res["error"])
        return res
    rebased = bool(rebase_res.get("rebased"))
    if rebased:
        _audit(task_svc, task_id, "rebase", "onto origin/main")

    # ---- push --------------------------------------------------------------
    # A clean rebase rewrites the branch's commit shas, so a plain push would
    # be refused as non-fast-forward -- force-with-lease (never bare --force)
    # only on that path, so a concurrent push to the SAME task branch by
    # something else is still refused rather than silently overwritten.
    push_argv = ["git", "push", "origin", branch]
    if rebased:
        push_argv.append("--force-with-lease")
    rc, out, err = _run(run, push_argv, path)
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
    # Set only once the FIRST "no checks reported" response is seen, so the
    # grace window is bounded from that first sighting, not from ci_wait's
    # own start (a few real-pending polls first must not eat into it).
    not_yet_registered_deadline: Optional[float] = None
    while True:
        rc, out, err = _run(run, ["gh", "pr", "checks", str(pr), *repo_flag], path)
        if rc == 0:
            break
        text = err or out or ""
        is_pending = (rc == GH_CHECKS_PENDING_RC
                     or _PENDING_STATUS_RE.search(text))
        if not is_pending:
            if _NO_CHECKS_REGISTERED_RE.search(text):
                if not_yet_registered_deadline is None:
                    not_yet_registered_deadline = (
                        time.monotonic() + float(not_yet_registered_grace_s))
                if time.monotonic() < not_yet_registered_deadline:
                    if poll_interval_s:
                        time.sleep(float(poll_interval_s))
                    continue
                res = _fail(
                    "ci_wait",
                    f"no checks were ever reported for PR #{pr} after "
                    f"{int(not_yet_registered_grace_s)}s grace: {text}", pr)
                _park(task_svc, task_id, res["stage"], res["error"])
                return res
            # Any OTHER ci_wait failure (a real check that ran and failed)
            # still fails immediately on its first poll -- unchanged.
            res = _fail("ci_wait", text or f"gh pr checks exited {rc}", pr)
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

    # RECORD THE TERMINAL NODE (task 8fbd5cf0). The branch is on
    # origin/main right now -- this is what "the token reaches the
    # terminal node" means: the work landed, not that green_gate merely
    # went green. Best-effort: a recorder error never blocks shipping.
    try:
        from prism_service.project_context import get_project
        from prism_service.services.flow_run_recorder import (
            record_node_execution, SHIPPED_NODE)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        record_node_execution(
            str(get_project(project)._data_dir / "scores.db"),
            {"task_id": task_id, "node_id": SHIPPED_NODE,
             "actor": "ship-worker", "workflow_id": "conductor",
             "outcome": "pass", "reason": f"landed on origin/main (pr #{pr})",
             "started_at": now, "ended_at": now},
            project=project)
    except Exception:
        pass

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


_UNSHIPPED_BLOCK_MARKER = "commit trailer is not yet reachable from origin/main"
# The invariant half of the shipped-ness objection (conductor_service.py:2913)
# -- no task id, no em dash, no trailing advice, so a reworded tail does not
# silently empty the machine ship queue again. It is specific enough that no
# other blocked_reason writer produces it.

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
    # A task the shipped-ness tooth itself parked (the objection built at
    # conductor_service.py:2913, written into blocked_reason by
    # _park_green_refusal) is DEADLOCKED: the gate cannot pass until the
    # branch lands, and the branch cannot land while this scan reads only
    # in_progress. That objection is bookkeeping, not a defect in the work,
    # so it must not remove the task from the ship queue. Admission is a
    # POSITIVE match on that one objection -- never on status == "blocked",
    # which would put genuinely broken work (a rebase conflict, a red CI
    # check) back in the queue to fail again and starve healthy tasks.
    candidates = list(ctx.task_svc.list(status="in_progress"))
    for task in ctx.task_svc.list(status="blocked"):
        reason = str(getattr(task, "blocked_reason", "") or "")
        if _UNSHIPPED_BLOCK_MARKER in reason:
            candidates.append(task)
    out = []
    for task in candidates:
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
