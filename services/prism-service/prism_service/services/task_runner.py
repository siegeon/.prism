"""Server-side task runner — drives a task WITHOUT a human terminal
(epic 0784729f, AC-4).

THE GAP THIS CLOSES: the conductor state machine (models/workflow.py)
owns the STEP SEQUENCE, but nothing calls the loop verb unless a human's
Claude Code session sits at a terminal looping on `conductor_work`. The
board reads "paused" the moment nobody is present — not because the task
is blocked, but because the LOOP itself has no driver. This module IS
that driver: a background thread that plays the worker role
`conductor_work` expects, one eligible task and one step per tick, by
invoking the SAME `claude_cli.invoke` primitive every other background
inference call in this service already uses, then reporting through
`api/conductor_flow`'s `flow_start`/`flow_report` so every honest-gate
tooth (distinct-actor, proof-carrying artifact, worker contract) still
applies exactly as it does to a human-driven session.

OPT-IN, OFF by default (mirrors `gate_adjudicator`'s
PRISM_GATE_ADJUDICATOR_INTERVAL): an environment opts in with
PRISM_TASK_RUNNER_INTERVAL=<seconds>. With the var unset this module
allocates no thread, calls no `claude`, and costs nothing — production
behavior is byte-for-byte what it was before this file existed.

NEVER decides a gate: a task whose CURRENT step is type=="gate" is
skipped outright, whether pending, passed, or failed. Gate adjudication
is `gate_adjudicator`'s seat, with its own distinct-actor and evidence
rules; this runner only ever plays an AGENT step and hands the result to
the same `flow_report` a human session would call, which enforces every
rule (distinct-actor, worker contract, proof-carrying artifact) on its
own — this module adds no new gate logic of its own.

HOST-LOAD CIRCUIT BREAKER (real incident, 2026-08-26): this seat is
willing to spawn a real `claude -p` subprocess for every eligible task,
and this host runs MANY concurrent tasks/agents by design -- with no
check on actual host resource pressure, a heavily loaded night can pile
new subprocesses onto an already-saturated box (one background fix that
night was independently killed by its own internal timeout purely from
host contention). `_system_overloaded()` refuses new work, ACTIVE by
default whenever this seat is enabled (PRISM_TASK_RUNNER_MAX_LOAD_PER_
CORE, default 8.0 per core), the same two call sites as the spend
ceiling below.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from typing import Optional

DEFAULT_INTERVAL_S = 0  # OFF unless an environment explicitly opts in
SEAT_ID = "prism-task-runner"  # distinct-actor identity on every report
# Real coding work needs more than READ_ONLY_TOOLS (batch analyzers never
# touch disk) — a build step must read, write and run tests in the
# task's own worktree.
BUILD_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit", "Bash")
# Mirrors mcp/tools.py's conductor_work report routing exactly, so a
# runner-produced report reads identically to a human-driven one.
_PLAN_STEPS = ("draft_story", "verify_plan")
_PREMISE_STEP = "review_previous_notes"


def _scores_db_for(project: str) -> str:
    from prism_service.project_context import get_project

    return str(get_project(project)._data_dir / "scores.db")


def _interval_s() -> int:
    raw = os.environ.get("PRISM_TASK_RUNNER_INTERVAL", "")
    try:
        return int(raw) if raw.strip() else DEFAULT_INTERVAL_S
    except ValueError:
        return DEFAULT_INTERVAL_S


def _max_turns() -> int:
    try:
        return max(1, int(os.environ.get("PRISM_TASK_RUNNER_MAX_TURNS", "30")))
    except ValueError:
        return 30


def _max_budget_usd() -> float:
    try:
        return max(0.0, float(
            os.environ.get("PRISM_TASK_RUNNER_MAX_BUDGET_USD", "2.0")))
    except ValueError:
        return 2.0


def _step_timeout_s() -> float:
    """Wall-clock bound on a single claude_cli.invoke() call (epic
    3baadd19 AC-1): without this, a wedged `claude -p` child hangs the
    whole drive worker forever -- invoke() has supported timeout_s since
    af8ec904, but nothing called it with one until this wiring."""
    try:
        return max(1.0, float(
            os.environ.get("PRISM_TASK_RUNNER_STEP_TIMEOUT_S", "900")))
    except ValueError:
        return 900.0


def _max_total_usd() -> Optional[float]:
    """Aggregate spend ceiling across every tick this process ever runs
    (epic 3baadd19 AC-5) -- unlike `_max_budget_usd` (a PER-INVOCATION cap
    passed to claude_cli.invoke), this bounds the runaway case: a stuck
    loop, a wedged step retried forever, or simply many expensive tasks
    queued back to back. Unset/blank == unbounded (None), matching every
    other opt-in knob in this module."""
    raw = os.environ.get("PRISM_TASK_RUNNER_MAX_TOTAL_USD", "")
    if not raw.strip():
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _max_load_per_core() -> float:
    """Per-core 1-minute load-average ceiling for the host-overload circuit
    breaker (real incident, 2026-08-26: 30+ PRISM tasks sat `in_progress`
    simultaneously on this same host, each one this module is willing to
    spawn a real `claude -p` subprocess for, contending with many other
    concurrent agent/pytest processes already running -- one of that
    night's own background fixes was independently killed by its own
    internal timeout purely from host contention).

    Unlike `_max_total_usd` (an aggregate spend ceiling that is
    meaningless without an owner-chosen dollar figure, so it defaults to
    None/unbounded until explicitly set), a host-load ceiling has a sane
    universal default -- so this mirrors `_max_turns`/`_max_budget_usd`/
    `_step_timeout_s`'s shape instead: a real numeric default that is
    ACTIVE the moment task_runner itself is enabled, no second opt-in
    required. Deliberately NOT matching `_spend_ceiling_crossed`'s
    unset-means-unbounded posture: that would leave exactly the gap the
    owner asked to close (a heavily-loaded box with no guard at all until
    someone remembers a second env var).

    Default 8.0: this host idles around 0.15-0.2 per core (`uptime`
    observed load average 3.74 on 24 cores while quiet). Most concurrent-
    agent load is TIME SPENT BLOCKED ON NETWORK I/O (waiting on LLM
    calls), which Linux's load average does not count as runnable, so
    legitimate heavy multi-agent concurrency -- the normal, by-design
    state of this box -- should stay well under this ceiling. 8.0/core is
    reserved for genuine saturation (many CPU-runnable or disk-blocked
    processes queued for the same cores), the failure mode that silently
    killed one of tonight's background fixes via its own internal
    timeout.
    """
    try:
        return max(0.1, float(
            os.environ.get("PRISM_TASK_RUNNER_MAX_LOAD_PER_CORE", "8.0")))
    except ValueError:
        return 8.0


def _system_overloaded() -> bool:
    """True (and logged) once the 1-minute load average per CPU core
    exceeds `_max_load_per_core()`. Fails SAFE -- returns False, never
    raises -- on any platform/environment where `os.getloadavg()` is
    unavailable (Windows, some containers): exactly like
    `_spend_ceiling_crossed()` fails safe (False) when its own env var is
    unset, a check this module cannot perform must never be treated as a
    reason to refuse work."""
    try:
        load1, _, _ = os.getloadavg()
        cpu_count = os.cpu_count() or 1
    except (OSError, AttributeError, NotImplementedError):
        return False
    ceiling = _max_load_per_core()
    per_core = load1 / cpu_count
    if per_core <= ceiling:
        return False
    _log(f"host overloaded: {load1:.2f} 1-min load avg / {cpu_count} cores "
         f"= {per_core:.2f} per-core > {ceiling:.2f} ceiling "
         "(PRISM_TASK_RUNNER_MAX_LOAD_PER_CORE) -- refusing further work "
         "until load drops")
    return True


def _log(msg: str) -> None:
    print(f"[task-runner] {msg}", file=sys.stderr, flush=True)


def is_enabled() -> bool:
    """True when this environment opted into the seat (interval > 0)."""
    return _interval_s() > 0


# Reentrancy guard: never re-enter a task already being driven by this
# process's own runner — the tick loop is single-threaded so this only
# matters for overlapping in-process callers (e.g. concurrent tests).
_IN_FLIGHT: set[str] = set()
_IN_FLIGHT_LOCK = threading.Lock()

# Round-robin offset into get_all_projects()'s (alphabetically SORTED,
# config.py:list_projects) result -- without this, sweep_once always
# starts scanning from the same first project every tick, so a project
# that always has an eligible task (a continuous backlog) permanently
# starves every alphabetically-later project of the single per-tick work
# slot. Observed live (epic 3baadd19 AC-2, task 12403c60): "csregs-
# datamanagement" < "prism" sorts first, and its own continuously-
# refilled backlog held every tick for the runner's entire uptime while
# an eligible prism task sat untouched.
_RR_LOCK = threading.Lock()
_rr_index = 0


class _SpendTracker:
    """Process-lifetime accumulator of `usage["cost_usd"]` across every
    step this seat has run (epic 3baadd19 AC-5). Charged immediately after
    each claude_cli.invoke() returns, checked against `_max_total_usd()`
    before the NEXT tick claims any work."""

    def __init__(self) -> None:
        self.spent = 0.0
        self._lock = threading.Lock()

    def charge(self, amount: object) -> None:
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return
        with self._lock:
            self.spent += amount


_spend_tracker = _SpendTracker()


def _spend_ceiling_crossed() -> bool:
    """True (and logged) once accumulated spend has exceeded the
    configured ceiling -- unset/blank PRISM_TASK_RUNNER_MAX_TOTAL_USD is
    always False (unbounded)."""
    ceiling = _max_total_usd()
    if ceiling is None:
        return False
    spent = _spend_tracker.spent
    if spent <= ceiling:
        return False
    _log(f"spend ceiling crossed: ${spent:.2f} spent > ${ceiling:.2f} "
         "ceiling (PRISM_TASK_RUNNER_MAX_TOTAL_USD) -- refusing further "
         "work until the ceiling is raised")
    return True


def _claim(task_id: str) -> bool:
    with _IN_FLIGHT_LOCK:
        if task_id in _IN_FLIGHT:
            return False
        _IN_FLIGHT.add(task_id)
        return True


def _release(task_id: str) -> None:
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.discard(task_id)


def eligible_task(project: str) -> Optional[str]:
    """The id of the one task this tick may drive in `project`, or None.

    Eligible == in_progress and its CURRENT step is an AGENT step — a task
    sitting at a gate (pending, passed, or failed) is never eligible; gate
    adjudication belongs to a distinct seat, not this one. A task that has
    NOT YET entered the flow (workflow_step=="") is also eligible: WORKFLOW_
    STEPS[0] (review_previous_notes) is always type=="agent", never a gate,
    and _run_one_step's own flow_start call is what sets workflow_step for
    the first time — this seat must be the one to make that call, or a task
    that only ever gets `status=in_progress` (never separately driven by a
    human/session into the flow) sits forever with workflow_step=="" and is
    never eligible under any check that requires it non-empty first (epic
    3baadd19's own oracle: set in_progress and touch nothing else).
    """
    from prism_service.project_context import get_project
    from prism_service.services.conductor_service import ConductorService

    if _spend_ceiling_crossed():
        return None
    if _system_overloaded():
        return None

    ctx = get_project(project)
    for t in ctx.task_svc.list(status="in_progress"):
        if t.id in _IN_FLIGHT:
            continue
        if not t.workflow_step:
            return t.id
        step = ConductorService._step_by_id(t.workflow_step)
        if step is None or step["type"] == "gate":
            continue
        return t.id
    return None


def _route_proof(task_svc, task_id: str, step_id: str, proof: str) -> None:
    """Write a successful step's output to the field its gate rubric
    reads — the SAME routing conductor_work's MCP handler applies."""
    try:
        if step_id in _PLAN_STEPS:
            task_svc.update(task_id, plan_doc=proof, completion_proof=proof)
        elif step_id == _PREMISE_STEP:
            task_svc.update(task_id, premise_notes=proof,
                            completion_proof=proof)
        else:
            task_svc.update(task_id, completion_proof=proof)
    except Exception:
        pass


def run_one_step(project: str, task_id: str) -> dict:
    """Drive exactly one AGENT step of `task_id` end to end: fetch the
    job via `flow_start`, invoke `claude_cli` against the task's OWN
    worktree, and report the outcome via `flow_report`. Never raises —
    every failure path returns a result dict so a tick loop stays alive.
    """
    if not _claim(task_id):
        return {"ok": False, "task_id": task_id,
                "reason": "already in flight"}
    try:
        return _run_one_step(project, task_id)
    finally:
        _release(task_id)


def _run_one_step(project: str, task_id: str) -> dict:
    from prism_service.api import conductor_flow as flow
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace

    task_svc = get_project(project).task_svc
    started = flow.flow_start(
        flow.Ident(task_id=task_id, session_id=SEAT_ID), project=project)
    if not started.get("ok"):
        return {"ok": False, "task_id": task_id,
                "reason": started.get("error") or "flow_start refused"}
    job = started.get("job")
    if not job or job.get("kind") == "gate":
        return {"ok": False, "task_id": task_id,
                "reason": "no eligible agent job (gate or terminal)"}

    ws = task_workspace.workspace_for(task_id) or {}
    work_dir = ws.get("path")
    if not work_dir:
        return {"ok": False, "task_id": task_id, "step": job["step"],
                "reason": "no workspace on file for task"}

    from prism_service.inference import claude_cli
    from prism_service.services import drive_heartbeat

    scores_db = _scores_db_for(project)
    drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": task_id, "step": job["step"], "elapsed_s": 0,
        "last_tool": "claude_cli.invoke", "work_units": 1,
    })
    try:
        result = claude_cli.invoke(
            job["instructions"], work_dir=work_dir, plugin_dir=work_dir,
            max_turns=_max_turns(), max_budget_usd=_max_budget_usd(),
            timeout_s=_step_timeout_s(),
            allowed_tools=BUILD_TOOLS, project=project,
            purpose=f"task-runner@{job['step']}#{task_id[:8]}",
            session_id=str(uuid.uuid4()))
    except Exception as exc:
        return {"ok": False, "task_id": task_id, "step": job["step"],
                "reason": f"claude_cli invocation failed: {exc}"}

    # The run's OWN usage, straight off the `result` stream event that
    # claude_cli already parsed for us. This seat reports under a SEAT NAME,
    # not a session UUID, so nothing downstream can recover these figures from
    # a transcript -- carrying them here is the only way a bot step's tokens,
    # model and cost ever reach its agent_runs row (task 9a51e670).
    # getattr-guarded: a lighter result object must never break the drive.
    usage = getattr(result, "usage", None)
    usage = dict(usage) if isinstance(usage, dict) and usage else None
    # Charged IMMEDIATELY after invoke() returns (epic 3baadd19 AC-2/AC-6):
    # this step already started, so it must still complete and report --
    # only the FOLLOWING tick's pre-claim check sees the crossed ceiling.
    if usage and usage.get("cost_usd"):
        _spend_tracker.charge(usage["cost_usd"])

    proof = (result.final_text() or "").strip()
    step_id = job["step"]
    # A graceful budget/turn ceiling (exit!=0 but the model's own turn
    # ended normally -- see ClaudeCliResult.graceful_budget_stop) still
    # carries a complete, usable report and must not be discarded just
    # because the post-hoc budget check marked the run is_error. Any OTHER
    # non-zero exit (crash, auth failure, truncation mid-generation) keeps
    # failing exactly as before.
    if proof and (result.exit_code == 0 or result.graceful_budget_stop()):
        _route_proof(task_svc, task_id, step_id, proof)
        outcome: object = "pass"
    elif not proof:
        outcome = {"ok": False,
                   "reason": f"exit={result.exit_code}, no usable output"}
    else:
        outcome = {"ok": False,
                   "reason": f"exit={result.exit_code}, non-graceful "
                             "failure (crash/auth/truncated mid-turn)"}

    report = flow.flow_report(flow.Ident(
        task_id=task_id, session_id=SEAT_ID, outcome=outcome,
        expected_step=step_id, usage=usage,
        model=(usage or {}).get("model") or None), project=project)
    return {"ok": bool(report.get("ok")), "task_id": task_id,
            "step": step_id, "run_id": result.run_id,
            "tokens": (int((usage or {}).get("input_tokens") or 0)
                       + int((usage or {}).get("output_tokens") or 0)),
            "cost_usd": float((usage or {}).get("cost_usd") or 0.0),
            "report": report}


def sweep_once() -> Optional[dict]:
    """One pass over every project, starting from a ROTATING offset: drive
    the first eligible task found and stop — AT MOST one task advances per
    tick, bounding blast radius. The starting project rotates every tick
    (module-level _rr_index) so no single project can monopolize the one
    work slot forever just by sorting first and always having something
    eligible. Returns the run_one_step result, or None when nothing was
    eligible.
    """
    from prism_service.project_context import get_all_projects

    if _spend_ceiling_crossed():
        return None
    if _system_overloaded():
        return None

    global _rr_index
    projects = get_all_projects()
    if not projects:
        return None
    with _RR_LOCK:
        start = _rr_index % len(projects)
    ordered = projects[start:] + projects[:start]

    for pid in ordered:
        try:
            task_id = eligible_task(pid)
        except Exception as exc:
            _log(f"{pid}: eligibility check failed: {exc}")
            continue
        if task_id is None:
            continue
        res = run_one_step(pid, task_id)
        _log(f"{pid}/{task_id[:8]}: {res}")
        with _RR_LOCK:
            # Next tick starts AFTER this project -- the actual round-robin
            # advance. Re-index against the CURRENT `projects` list (not
            # `ordered`) so a project add/remove between ticks can't skew
            # the offset.
            _rr_index = (projects.index(pid) + 1) % len(projects)
        return res
    return None


# A task becoming eligible (status -> in_progress, or a step/gate transition)
# is a REAL event -- task_service.update() already publishes it as
# "task.changed" for /sse/tasks. Until this existed, this runner's only
# way to notice was to wake up on a bare interval() and poll, so a task
# that became the ONLY eligible thing in the whole project could still
# sit untouched for the full interval (observed live: 900s/~16min wait for
# a single, uncontested task). `wake()` is called from that same publish
# path (task_service.py) so the loop below reacts to the real event instead
# of a fixed clock; the interval survives ONLY as a safety-net upper bound
# for events this module never learns about (a direct DB write, a crash
# recovery, etc.) -- never the primary trigger anymore.
_wake_event = threading.Event()


def wake() -> None:
    """Nudge the runner loop to sweep now instead of waiting out its
    interval. Safe to call even when the runner was never started
    (PRISM_TASK_RUNNER_INTERVAL unset) -- just sets a flag nobody reads."""
    _wake_event.set()


def _loop(interval_s: int, stop_event: Optional[threading.Event] = None) -> None:
    """`stop_event` is test-only plumbing (never passed by
    `start_task_runner`): without it, a test that spins up this loop in a
    background thread has no way to end it, so the thread outlives the
    test and its calls to the shared, module-level `sweep_once` keep
    firing on every later test's `wake()` -- observed live as exactly
    that leak in test_wake_cuts_the_wait_short_instead_of_sitting_out_the_
    interval, which used to cope by permanently stubbing `sweep_once` to
    a no-op instead, breaking every later test that calls it directly."""
    _log(f"started; interval={interval_s}s (event-driven, interval is a fallback)")
    while stop_event is None or not stop_event.is_set():
        try:
            sweep_once()
        except Exception as exc:
            _log(f"sweep error: {exc}")
        if _wake_event.wait(timeout=interval_s):
            _wake_event.clear()


def start_task_runner() -> Optional[threading.Thread]:
    """Spawn the task-runner daemon thread, unless disabled via
    PRISM_TASK_RUNNER_INTERVAL unset/<=0 (the default). Mirrors
    start_gate_adjudicator: same shape, same off-by-default posture."""
    interval = _interval_s()
    if interval <= 0:
        _log("disabled (default OFF; set PRISM_TASK_RUNNER_INTERVAL="
             "<seconds> to opt this environment in)")
        return None
    t = threading.Thread(target=_loop, args=(interval,),
                         name="prism-task-runner", daemon=True)
    t.start()
    return t
