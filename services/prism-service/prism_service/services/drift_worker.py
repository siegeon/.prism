"""Background drift reindexer -- runs Brain.incremental_reindex ONLY for
projects someone is actually looking at right now (task: livehang round 4).

Timeline: the OLD start_drift_timer (main.py) swept EVERY tracked project
(get_all_projects(), ~30 slugs on the live instance, including junk/dead
ones nobody opens) on a fixed 30-minute cadence -- and its very first tick
after any restart always fired immediately (`last_reindex` started at
0.0), so a fresh daemon spent 20-30 minutes at 150-180% CPU sweeping every
project before it ever settled, starving every OTHER thread's access to
the GIL (confirmed live: /api/workflows, /api/conductor/state and /api/
work/graph all hung >20s during this window while /api/version stayed at
10ms, and it recurred on every restart). Separately, `incremental_reindex`
ran its `git diff`/`git ls-files` calls with NO `cwd` -- so every project's
Brain instance diffed the DAEMON'S OWN checkout, not that project's own
repo, which is why nearly every one of ~30 projects logged an identical
"reindexed 65 drifted file(s)" (see brain_engine.py's incremental_reindex
docstring for that half of the fix).

Owner directive (2026-09-13, verbatim intent): "we only care about the
project we have open... stop doing work just for fun... the whole thing
is supposed to be fast and buttery smooth." This module is the reshaped
worker: `sweep_once()` is gated to only reindex a project that has had a
real client request recently (services/project_activity.py) or has an
in-progress task, only runs while the API has been idle for a few
seconds, and bounds itself by a wall-clock budget per call -- any
projects left over resume on the NEXT call rather than blocking to
finish. Every project whose configured source path does not resolve to a
real directory is dropped from consideration (logged once) and never
re-checked.

ROUND 6: fixing WHICH projects got swept (this module) and per-file
content hashing (brain_engine.py's incremental_reindex) still left one
thing unbounded -- a project that stays "in use" continuously (the
normal case: someone actively working a checkout with real, ongoing
uncommitted edits) got swept every ~5s forever, and even with content
hashing most of those passes correctly found NOTHING new to embed.
Cheap per pass, but "cheap x every 5 seconds forever" is still real,
sustained CPU (observed live: ~147% sustained). Per-project exponential
backoff (`_backoff`) means a pass that embeds zero files pushes that
project's NEXT eligible sweep out (60s, then 120s, ... capped at 15
minutes); a pass that DOES embed something resets it to immediate
eligibility again, so genuinely active editing still gets swept
promptly while a quiet-but-still-open project stops being re-checked
every few seconds for no reason. (A dedicated filesystem watcher would
be the more precise "wakeup signal" -- out of scope here; a real content
change is the wakeup signal this round implements, which is also the
one signal that can never miss a genuine edit.)

main.py's start_drift_timer is now a thin `while True: sweep_once();
sleep(...)` wrapper -- this module is what tests exercise directly, same
convention as sweep_once() in gate_adjudicator.py / task_runner.py."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from prism_service.services import project_activity, system_activity

# How recently a project must have had a real client request to count as
# "in use" -- 10 minutes covers a normal working session with gaps for
# reading/thinking without re-sweeping the instant a tab goes quiet.
ACTIVE_WINDOW_S = float(os.environ.get("PRISM_DRIFT_ACTIVE_WINDOW_S", "600"))
# Only run while the API has been idle (no request to ANY project) for at
# least this long -- never compete with a live request burst.
IDLE_GATE_S = float(os.environ.get("PRISM_DRIFT_IDLE_GATE_S", "3"))
# Wall-clock ceiling per sweep_once() call -- a project mid-reindex when
# the budget expires still finishes (a single incremental_reindex() call
# is not itself preemptible), but no NEW project starts once it's spent;
# leftover candidates resume on the next call.
BUDGET_S = float(os.environ.get("PRISM_DRIFT_BUDGET_S", "5"))
# Per-project backoff after a pass that embeds zero files (task: livehang
# round 6) -- doubles each consecutive zero-change pass, capped at 15
# minutes; a pass that DOES embed something resets it to zero (next
# sweep eligible immediately).
BACKOFF_MIN_S = float(os.environ.get("PRISM_DRIFT_BACKOFF_MIN_S", "60"))
BACKOFF_MAX_S = float(os.environ.get("PRISM_DRIFT_BACKOFF_MAX_S", "900"))

_drift_brains: dict = {}
_dropped: set[str] = set()
_pending: list[str] = []
_backoff: dict[str, dict] = {}  # pid -> {"next_at": monotonic ts, "level_s": float}


def _backoff_ready(pid: str) -> bool:
    st = _backoff.get(pid)
    return st is None or time.monotonic() >= st["next_at"]


def _update_backoff(pid: str, embedded: int) -> None:
    if embedded > 0:
        _backoff.pop(pid, None)  # a real change just happened -- stay fast
        return
    prev_level = _backoff.get(pid, {}).get("level_s", 0.0)
    level = min(BACKOFF_MAX_S, prev_level * 2 if prev_level else BACKOFF_MIN_S)
    _backoff[pid] = {"next_at": time.monotonic() + level, "level_s": level}


def _has_in_progress_task(pid: str) -> bool:
    from prism_service.project_context import get_project
    try:
        return bool(get_project(pid).task_svc.active_ids())
    except Exception:
        return False


def _resolve_repo_path(pid: str) -> str:
    from prism_service.services.claude_transcripts import _project_source_path
    try:
        return _project_source_path(pid) or ""
    except Exception:
        return ""


def sweep_once() -> list[dict]:
    """One drift-reindex pass. Returns a list of {"project", "files",
    "elapsed_ms"} for each project actually reindexed this call -- empty
    when idle-gated (a request landed too recently) or no project
    currently qualifies as in-use."""
    if not project_activity.idle_for(IDLE_GATE_S):
        return []

    from prism_service.project_context import get_all_projects, get_project
    from prism_service.engines.brain_engine import Brain

    live = set(get_all_projects())
    for stale in [p for p in _drift_brains if p not in live]:
        brain = _drift_brains.pop(stale, None)
        for name in ("close", "shutdown"):
            fn = getattr(brain, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
                break
        print(f"[drift] released stale Brain for {stale}", file=sys.stderr)

    candidates = [
        p for p in live
        if p not in _dropped
        and _backoff_ready(p)
        and (project_activity.seen_within(p, ACTIVE_WINDOW_S)
             or _has_in_progress_task(p))
    ]
    if _pending:
        queue = [p for p in _pending if p in candidates] + \
                [p for p in candidates if p not in _pending]
    else:
        queue = candidates
    _pending.clear()

    results: list[dict] = []
    pass_start = time.monotonic()
    for idx, pid in enumerate(queue):
        if time.monotonic() - pass_start >= BUDGET_S:
            _pending.extend(queue[idx:])
            break

        repo_path = _resolve_repo_path(pid)
        if not repo_path or not Path(repo_path).is_dir():
            if pid not in _dropped:
                _dropped.add(pid)
                print(
                    f"[drift] {pid}: no resolvable project root -- "
                    "dropped from tracking",
                    file=sys.stderr,
                )
            continue

        ctx = get_project(pid)
        db_dir = ctx._data_dir
        brain = _drift_brains.get(pid)
        if brain is None:
            brain = Brain(
                brain_db=str(db_dir / "brain.db"),
                graph_db=str(db_dir / "graph.db"),
                scores_db=str(db_dir / "scores.db"),
                tasks_db=str(db_dir / "tasks.db"),
            )
            _drift_brains[pid] = brain

        from prism_service.services import wakeups

        t0 = time.monotonic()
        stats: dict = {}
        # Serialized with the other pure-maintenance sweeps (see
        # dispatch_guard._loop's comment) -- never task_runner/
        # resume_actuator/ship_worker/gate_adjudicator.
        with wakeups.serial_slot():
            with system_activity.pass_(
                    "drift_reindex", pid, "incremental_reindex") as info:
                n = brain.incremental_reindex(repo_path=repo_path, stats=stats)
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                detail = (
                    f"{stats.get('candidates', 0)} candidates, "
                    f"{stats.get('changed', 0)} changed, "
                    f"{stats.get('embedded', 0)} embedded, {elapsed_ms:.0f}ms"
                )
                info["detail"] = detail
                # A clean project (n==0, the common case once its own
                # baseline has settled) collapses into the throttled idle
                # entry instead of one ring-buffer line per in-use project
                # per tick.
                info["active"] = bool(n)
        _update_backoff(pid, stats.get("embedded", 0))
        print(f"[drift] {pid}: {detail}", file=sys.stderr)
        results.append({"project": pid, "files": n, "elapsed_ms": elapsed_ms})
        # Task fix/lasttimers: a completed drift-reindex pass is one of the
        # events that can move /api/staleness's graph/brain booleans --
        # signal it so WorkflowsPage's brain-activity panel refetches on
        # the real event instead of a fixed-interval poll.
        try:
            wakeups.signal("staleness", pid or "*")
        except Exception:
            pass

    return results


def reset_for_tests() -> None:
    """Test-only: clear module-level state between tests (this module's
    Brain cache / dropped-project set / resume queue are process-lifetime
    singletons in production, same as the OLD start_drift_timer's local
    dict was for its own process's lifetime)."""
    _drift_brains.clear()
    _dropped.clear()
    _pending.clear()
    _backoff.clear()
