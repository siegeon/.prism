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
"""
from __future__ import annotations

import os
import sys
import threading
import time
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

    Eligible == in_progress, has already entered the flow (a non-empty
    workflow_step), and its CURRENT step is an AGENT step — a task
    sitting at a gate (pending, passed, or failed) is never eligible;
    gate adjudication belongs to a distinct seat, not this one.
    """
    from prism_service.project_context import get_project
    from prism_service.services.conductor_service import ConductorService

    ctx = get_project(project)
    for t in ctx.task_svc.list(status="in_progress"):
        if not t.workflow_step or t.id in _IN_FLIGHT:
            continue
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
    try:
        result = claude_cli.invoke(
            job["instructions"], work_dir=work_dir, plugin_dir=work_dir,
            max_turns=_max_turns(), max_budget_usd=_max_budget_usd(),
            allowed_tools=BUILD_TOOLS, project=project,
            purpose=f"task-runner@{job['step']}#{task_id[:8]}")
    except Exception as exc:
        return {"ok": False, "task_id": task_id, "step": job["step"],
                "reason": f"claude_cli invocation failed: {exc}"}

    proof = (result.final_text() or "").strip()
    step_id = job["step"]
    if result.exit_code == 0 and proof:
        _route_proof(task_svc, task_id, step_id, proof)
        outcome: object = "pass"
    else:
        outcome = {"ok": False,
                   "reason": f"exit={result.exit_code}, no usable output"}

    # The run's OWN usage, straight off the `result` stream event that
    # claude_cli already parsed for us. This seat reports under a SEAT NAME,
    # not a session UUID, so nothing downstream can recover these figures from
    # a transcript -- carrying them here is the only way a bot step's tokens,
    # model and cost ever reach its agent_runs row (task 9a51e670).
    # getattr-guarded: a lighter result object must never break the drive.
    usage = getattr(result, "usage", None)
    usage = dict(usage) if isinstance(usage, dict) and usage else None

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
    """One pass over every project: drive the first eligible task found
    and stop — AT MOST one task advances per tick, bounding blast radius.
    Returns the run_one_step result, or None when nothing was eligible.
    """
    from prism_service.project_context import get_all_projects

    for pid in get_all_projects():
        try:
            task_id = eligible_task(pid)
        except Exception as exc:
            _log(f"{pid}: eligibility check failed: {exc}")
            continue
        if task_id is None:
            continue
        res = run_one_step(pid, task_id)
        _log(f"{pid}/{task_id[:8]}: {res}")
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


def _loop(interval_s: int) -> None:
    _log(f"started; interval={interval_s}s (event-driven, interval is a fallback)")
    while True:
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
