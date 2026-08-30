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
import re
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
# Stall detection (task 82cc05ee): after this many non-advancing reports
# on ONE step, the next tick does NOT spawn another identical attempt --
# it splits the red test ids named in the last proof into children, or
# blocks the task with a reason naming the step. The count lives in task
# history (action=ATTEMPT_ACTION), never in process memory.
STALL_ATTEMPTS = 3
ATTEMPT_ACTION = "runner_attempt"
_TEST_ID_RE = re.compile(r"(?m)(?:^|\s)((?:[\w./-]+)\.py::[\w\[\]./:-]+)")


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


# Steps that RUN THINGS rather than write about them. verify_green_state
# executes the whole suite, and implement_tasks builds and re-runs it, so
# the wall clock a paragraph needs is not the wall clock they need. The
# implement workflow already knew this ("known-slow steps get a multiple of
# it"); the task runner did not, and epic 9f60a849 stalled three times at
# verify_green_state with exit=-9 -- SIGKILL at the 900 s bound, on a host
# with 85 GB free and no OOM kills, so it was the clock and nothing else.
_SLOW_STEPS = {"verify_green_state": 3.0, "implement_tasks": 2.0}


def _step_timeout_s(step_id: str = "") -> float:
    """Wall-clock bound on a single claude_cli.invoke() call (epic
    3baadd19 AC-1): without this, a wedged `claude -p` child hangs the
    whole drive worker forever -- invoke() has supported timeout_s since
    af8ec904, but nothing called it with one until this wiring."""
    try:
        base = max(1.0, float(
            os.environ.get("PRISM_TASK_RUNNER_STEP_TIMEOUT_S", "900")))
    except ValueError:
        base = 900.0
    # The operator's variable still governs the base; the multiplier only
    # says which steps need more of whatever budget they chose.
    return base * _SLOW_STEPS.get(str(step_id or ""), 1.0)


# ----------------------------------------------------------------------
# Declared node plans (task 8848089d)
# ----------------------------------------------------------------------
# Every conductor node declares an execution plan in
# .prism/behaviors/conductor/<behavior>.json: which model, how many turns,
# what budget, and which sub-steps are CODIFIED -- deterministic Python
# that costs no tokens. This worker used to ignore all of it and fire ONE
# claude_cli.invoke per step with the blanket 30-turn / $2.00 / 900 s
# defaults, so services/premise_gather.py (task cd33263f) had no caller
# outside its own unit tests and review_previous_notes -- the FIRST step of
# every task -- grepped the repo cold on every drive (3,762 tokens, 1,725 s
# mean, against a declared 180 s bound).
#
# Only review_previous_notes opts in today, and DELIBERATELY so: it is the
# one behavior with a hand-tuned v2 plan and real codified sub-steps. The
# other five agentic behaviors all carry the SAME template budget (haiku /
# 4 turns / $0.50 / 120 s), which is boilerplate rather than a considered
# figure -- implement_tasks alone has a 474 s median and needs far more
# than four turns, so honouring that template would break every build step.
# A node joins this set when its declaration is real, never by default.
_PLANNED_STEPS = frozenset({"review_previous_notes"})

# Behavior ids per conductor step. Mirrors api/workflows._BEHAVIOUR_FOR_STEP
# for the steps THIS seat drives; the file is read straight off disk rather
# than through the AosWorkflows engine, so a drive never depends on that
# separate service being reachable.
_BEHAVIOR_FOR_STEP = {
    "review_previous_notes": "review-previous-notes-loop",
    "draft_story": "draft-story-loop",
    "verify_plan": "verify-plan-loop",
    "write_failing_tests": "write-failing-tests-loop",
    "implement_tasks": "implement-tasks-loop",
    "verify_green_state": "validation",
}

# A /steps/* route that calls a model. Everything else on a behavior is
# codified by construction (see api/workflows.py -- reason-loop is the one
# generic agentic endpoint, premise-judge the one bespoke one).
_AGENTIC_ROUTES = frozenset({"reason-loop", "premise-judge"})


def _behavior_dir(project: str):
    """The on-disk conductor behavior directory for `project`."""
    from pathlib import Path
    from prism_service.services.claude_transcripts import _project_source_path

    configured = Path(_project_source_path(project))
    fallback = Path.home() / "projects" / project
    root = configured if configured.is_absolute() and configured.exists() \
        else fallback
    return root / ".prism" / "behaviors" / "conductor"


def _node_plan(project: str, step_id: str) -> Optional[dict]:
    """The declared execution plan for `step_id`, or None.

    None means "no credible declaration" -- either the step is not in
    _PLANNED_STEPS, or its behavior file is missing/unreadable. Every
    caller then keeps this module's own defaults, so a malformed JSON can
    never make a drive cheaper than it should be, only unchanged.
    """
    import json

    if step_id not in _PLANNED_STEPS:
        return None
    behavior = _BEHAVIOR_FOR_STEP.get(step_id)
    if not behavior:
        return None
    path = _behavior_dir(project) / f"{behavior}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    codified: list[str] = []
    model: Optional[str] = None
    max_turns: Optional[int] = None
    budget: Optional[float] = None
    for step in doc.get("steps") or []:
        url = step.get("url") or ""
        route = url.split("/steps/")[-1].split("?")[0] if "/steps/" in url else ""
        if not route:
            continue
        if route not in _AGENTIC_ROUTES:
            codified.append(route)
            continue
        # The agentic middle carries the budget, as a JSON string body.
        try:
            body = json.loads(step.get("body") or "{}")
        except Exception:
            body = {}
        model = body.get("model") or model
        if isinstance(body.get("max_turns"), int):
            max_turns = body["max_turns"]
        if isinstance(body.get("max_budget_usd"), (int, float)):
            budget = float(body["max_budget_usd"])
    return {"model": model, "max_turns": max_turns,
            "max_budget_usd": budget, "codified": codified}


def _invoke_budget(step_id: str, plan: Optional[dict]) -> dict:
    """claude_cli.invoke kwargs for `step_id` under `plan`.

    The declared plan governs the MODEL, TURN LIMIT and BUDGET -- the three
    caps that actually bound spend. The wall clock stays this module's:
    _step_timeout_s already encodes hard-won knowledge about which steps
    run things rather than write about them, and a timeout that is too
    small kills a drive outright while one that is too large costs nothing
    once the turn and budget caps bind first.
    """
    runner_timeout = _step_timeout_s(step_id)
    if not plan:
        return {"model": "", "max_turns": _max_turns(),
                "max_budget_usd": _max_budget_usd(),
                "timeout_s": runner_timeout}
    # ONLY THE MODEL IS SAFE TO ADOPT. A behavior's turn/budget caps are
    # sized for the behavior's OWN narrow prompt (premise-judge merely
    # rules on facts that premise-gather already resolved). This worker
    # still sends job["instructions"] -- the full step brief -- so those
    # caps do not fit it. Proven live on task 6a7105f9 (2026-08-30): with
    # the declared max_turns=2 applied to the full brief, three attempts
    # each spent their turns on "Let me fetch the task details..." and
    # died `exit=1 ... truncated mid-turn`, and the task blocked at
    # review_previous_notes. The saving is real without them: haiku
    # instead of the default model, and a gather that removes the tool
    # round trips the model used to spend finding its own citations.
    # A future slice that also adopts the declared PROMPT may then adopt
    # the caps that were written for it -- never one without the other.
    return {
        "model": plan.get("model") or "",
        "max_turns": _max_turns(),
        "max_budget_usd": _max_budget_usd(),
        "timeout_s": runner_timeout,
    }


def _codified_preamble(project: str, task, memory_svc=None, task_svc=None,
                       brain_svc=None):
    """Resolved citations for `task`, as (preamble_text, facts).

    This is the CODIFIED gather sub-step running in-process: pure local
    reads, no model call, and no git/worktree lock (the 2026-08-29 daemon
    wedge this must not reproduce). Handing the judge real file:line
    citations up front is the whole token saving -- without it the model
    greps the repo cold, one tool round trip per claim.

    Degrades to ("", []) on ANY failure: a broken gather must leave the
    drive exactly as it was, never halt it.
    """
    try:
        from prism_service.services import premise_gather

        facts = premise_gather.gather(
            task, memory_svc=memory_svc, task_svc=task_svc,
            brain_svc=brain_svc)
    except Exception:
        return "", []
    if not facts:
        return "", []
    lines = [f"- [{f.kind}] {f.text} ({f.citation})" for f in facts]
    preamble = (
        "Grounded facts already resolved for this task (codified gather -- "
        "reuse a citation verbatim rather than searching for it again):\n"
        + "\n".join(lines))
    return preamble, list(facts)


def _record_codified_run(project: str, task_id: str, route: str,
                         run_id: str, ok: bool, summary: str) -> None:
    """Report a codified sub-step as its own ZERO-TOKEN agent_runs row.

    Without this the saving is invisible: the Workflows node card would
    keep reporting only the agentic middle, and nobody could tell the
    codified half ever ran.
    """
    try:
        from prism_service.services import agent_runs_data

        agent_runs_data.upsert_agent_run(_scores_db_for(project), {
            "run_id": run_id, "workflow_name": "implement",
            "task_id": task_id, "agent_id": SEAT_ID, "role": "sm",
            "step": route, "model": "codified", "tokens": 0,
            "cost_usd": 0.0, "ok": 1 if ok else 0,
            "verdict_summary": summary[:500],
        })
    except Exception:
        pass


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


# The value this seat writes into a drive heartbeat's `driver` column, and
# the ONE driver name eligible_task ignores when it decides whether somebody
# else is already on a task (task e4c631d7). The runner beats for itself at
# the start of every step, and a step transition wakes the next tick well
# inside HEARTBEAT_WINDOW_S -- a guard that only asked "is there a beat"
# would make this seat skip its own work forever.
RUNNER_DRIVER = "prism-task-runner"


def _foreign_driver_on(project: str, task_id: str) -> str:
    """The name of a DIFFERENT driver that beat for `task_id` inside
    drive_heartbeat.HEARTBEAT_WINDOW_S, or "" when nobody else is on it.

    This is the evidence half of the owner's "two queues of control"
    complaint (task d9f082fe): a session driving a task over the REST flow
    now marks it in_progress, which is what makes it visible on the board --
    and would also make it claimable here. A live foreign beat says the task
    already has a driver, so this seat stands down.
    """
    from prism_service.services import drive_heartbeat
    try:
        beat = drive_heartbeat.latest(_scores_db_for(project), task_id)
    except Exception:
        return ""
    if beat is None:
        return ""
    if beat.get("age_s") is None or beat["age_s"] > drive_heartbeat.HEARTBEAT_WINDOW_S:
        return ""
    driver = str(beat.get("driver") or "")
    return "" if driver in ("", RUNNER_DRIVER) else driver


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
        # Somebody else is demonstrably driving this task right now. Skipping
        # is not a refusal to work -- the beat goes stale within
        # HEARTBEAT_WINDOW_S of that driver stopping, and the task becomes
        # eligible again on a later tick.
        foreign = _foreign_driver_on(project, t.id)
        if foreign:
            _log(f"skipping {t.id[:8]}: driver {foreign!r} is live on it")
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


def _stall_count(task_svc, task_id: str, step_id: str) -> int:
    """Non-advancing runner reports recorded on `step_id` (durable)."""
    marker = f"step={step_id}; advanced=false"
    return sum(1 for h in task_svc.history(task_id)
               if h.action == ATTEMPT_ACTION and marker in h.details)


def red_test_ids(proof: str) -> list[str]:
    """Distinct pytest node ids named in a proof, in first-seen order."""
    seen: list[str] = []
    for m in _TEST_ID_RE.findall(proof or ""):
        if m not in seen:
            seen.append(m)
    return seen


def _shipped_sha_for_stall(task_id: str) -> str:
    """The origin/main commit carrying this task's own trailer, or "".

    Delegates to the SAME squash-safe reader the gate's unshipped tooth
    uses (api/tasks._shipped_sha_on_main), so the stall path and the gate
    can never disagree about whether a task landed.
    """
    from prism_service.api.tasks import _shipped_sha_on_main
    from prism_service.services.task_workspace import _prism_repo_root
    return _shipped_sha_on_main(str(_prism_repo_root()), task_id) or ""


def _stall_work_is_shipped(task_id: str) -> bool:
    """True when this task's work is already on origin/main.

    FAILS CLOSED: any error answers False, so a git or repo problem leaves
    the original split behaviour untouched rather than closing a task on an
    exception.
    """
    try:
        return bool(_shipped_sha_for_stall(task_id))
    except Exception:  # noqa: BLE001 - a stall handler never raises
        return False


def _last_outcome_was_a_kill(task_svc, task_id: str, step_id: str) -> bool:
    """True when this step's most recent recorded outcome was a SIGKILL.

    Reads the durable history rather than in-memory state, so a restart
    between the kill and the stall still tells the truth. Any error answers
    False, which keeps the original message: a stall handler never raises,
    and a wrong "killed" claim would be its own dishonesty.
    """
    try:
        marker = f"step={step_id}"
        for row in reversed(list(task_svc.history(task_id))):
            details = str(getattr(row, "details", "") or "")
            if getattr(row, "action", "") != "flow_report_failure":
                continue
            if marker not in details:
                continue
            return "exit=-9" in details
    except Exception:  # noqa: BLE001 - honesty is best-effort, never fatal
        return False
    return False


def _green_gate_ever_passed(task_svc, task_id: str) -> bool:
    """True when this task's OWN history carries a green_gate decision that
    approved. Shipped-ness proves the work exists; only this proves somebody
    judged it. FAILS CLOSED: any error answers False, so a history problem
    refuses to close a task rather than closing one nobody adjudicated."""
    try:
        rows = task_svc.history(task_id) or []
    except Exception:  # noqa: BLE001 - a stall handler never raises
        return False
    for r in rows:
        if getattr(r, "action", "") != "gate_decide":
            continue
        d = str(getattr(r, "details", "") or "")
        if "green_gate" in d and "action=approve" in d:
            return True
    return False


def _handle_stall(task_svc, task_id: str, step_id: str) -> dict:
    """Fourth tick on a stalled step: close if shipped, else decompose or
    block, never invoke.

    THE SHIPPED CASE FIRST (live waste, 2026-08-29): task 4cbac65a's fix
    landed on origin/main as b2f8d88f, out of band. The runner kept driving
    its verify_green_state step, stalled, and split it into SIX children --
    one per pinned test -- every one of those tests already green on main.
    Six pieces of work that could only ever be waste, in a queue that
    drives one step per tick. A task whose own trailer is reachable from
    origin/main is finished; the honest move is to close it.
    """
    if _stall_work_is_shipped(task_id):
        sha = _shipped_sha_for_stall(task_id)
        # NEVER CLOSE OVER AN UNDECIDED GATE (task 8fbd5cf0, 2026-08-30).
        # TaskService refuses status=done while a gate is open, but that
        # guard reads is_open_gate_step(), which only fires when the
        # workflow_step ITSELF is a gate. A rewind moves the task back to an
        # AGENT step and leaves gate_state='pending' behind, so the row
        # carries an open gate the guard cannot see -- and this path calls
        # task_svc.update directly, past the route-level check. Observed
        # live: 8fbd5cf0 went status=done, workflow_step=implement_tasks,
        # gate_state=pending, i.e. closed while its green_gate had never
        # been decided. Shipped-ness is not a verdict; a gate is.
        # SHIPPED IS NOT ADJUDICATED (task 8fbd5cf0, closed falsely THREE
        # times on 2026-08-30). Checking only for an OPEN gate is not enough:
        # a task sitting on an AGENT step has gate_state="none", so this path
        # closed it on shipped-ness alone while its driver was still working
        # and its green_gate had never been decided. Observed at 08:53:05 —
        # the runner closed the ticket while its driver was actively landing
        # commits (06b6b28c, 5ed871ff) minutes earlier. Require a real
        # green_gate PASS in this task's own history before shipped-ness may
        # close it. A trailer reaching origin/main says the work exists; only
        # a gate decision says anybody judged it.
        _t_now = task_svc.get(task_id)
        _gate_state = str(getattr(_t_now, "gate_state", "") or "none")
        if not _green_gate_ever_passed(task_svc, task_id):
            reason = (f"step {step_id} did not advance after "
                      f"{STALL_ATTEMPTS} attempts and this task's work is "
                      f"on origin/main ({sha[:12]}), but its green_gate has "
                      f"never been decided — refusing to close. Shipped is "
                      f"not adjudicated.")
            task_svc.record_history(task_id, action="runner_stall",
                                    details=reason, actor=SEAT_ID)
            return {"ok": False, "task_id": task_id, "step": step_id,
                    "run_id": "", "tokens": 0, "cost_usd": 0.0,
                    "report": None, "reason": reason}
        if _gate_state in ("pending", "failed"):
            reason = (f"step {step_id} did not advance after "
                      f"{STALL_ATTEMPTS} attempts and this task's work is "
                      f"on origin/main ({sha[:12]}), but its gate is still "
                      f"undecided (gate_state={_gate_state}) -- refusing to "
                      f"close. Decide the gate first; shipped is not the "
                      f"same as adjudicated.")
            task_svc.record_history(task_id, action="runner_stall",
                                    details=reason, actor=SEAT_ID)
            return {"ok": False, "task_id": task_id, "step": step_id,
                    "run_id": "", "tokens": 0, "cost_usd": 0.0,
                    "report": None, "reason": reason}
        reason = (f"step {step_id} did not advance after {STALL_ATTEMPTS} "
                  f"attempts, but this task's work is already on "
                  f"origin/main ({sha[:12]}) -- closing instead of "
                  f"splitting it into children that cannot change anything.")
        task_svc.update(task_id, status="done", full_outcome_complete=True,
                        blocked_reason="")
        task_svc.record_history(task_id, action="runner_stall",
                                details=reason, actor=SEAT_ID)
        return {"ok": True, "task_id": task_id, "step": step_id, "run_id": "",
                "tokens": 0, "cost_usd": 0.0, "report": None,
                "stalled": {"action": "shipped", "children": [],
                            "reason": reason}}

    """Fourth tick on a stalled step: decompose or block, never invoke."""
    parent = task_svc.get(task_id)
    # ONLY THIS TASK'S OWN RED TESTS. `completion_proof` is a task-level
    # field holding whatever the last report wrote, INCLUDING a verify step's
    # note about unrelated suites that were already failing. Live on task
    # 72ccaf94 (2026-08-29) its proof named ten node ids from test_auto_updater,
    # test_lifespan_lock_recovery, test_pidfile_lifecycle and
    # test_task_page_bundle -- none of them this slice's work, none of them in
    # its `verify` -- and the splitter made a child for every one, then made
    # them AGAIN on the next stall: 30 children, 3 duplicate sets, all junk.
    # Intersect with the task's own pinned suite so a child can only ever be
    # created for a test this task is actually responsible for.
    pinned = [str(v) for v in (getattr(parent, "verify", []) or [])]

    def _is_ours(test_id: str) -> bool:
        # A task that pins NOTHING keeps the original behaviour: there is no
        # ownership signal to filter on, and refusing to split there would
        # retire a contract this change has no quarrel with.
        if not pinned:
            return True
        f = test_id.split("::", 1)[0].split("/")[-1]
        return any(f == p.split("/")[-1] for p in pinned)

    ids = [i for i in red_test_ids(getattr(parent, "completion_proof", "") or "")
           if _is_ours(i)]
    # IDEMPOTENT. A stall that fires twice must not double the board: skip any
    # test id an OPEN child already covers. 72ccaf94 reached 30 children as
    # three identical sets of ten before this existed.
    existing: set = set()
    try:
        for c in (task_svc.list(parent_id=task_id) or []):
            if getattr(c, "status", "") in ("cancelled", "done"):
                continue
            for v in (getattr(c, "verify", []) or []):
                existing.add(str(v))
    except Exception:
        pass
    ids = [i for i in ids if i not in existing]
    children: list[str] = []
    for test_id in ids:
        child = task_svc.create(
            title=f"Make {test_id.rsplit('::', 1)[-1]} green",
            description=f"Split from {task_id} at step {step_id}: "
                        f"{test_id} stayed red for {STALL_ATTEMPTS} attempts.",
            priority=getattr(parent, "priority", 0) or 0,
            tags=list(getattr(parent, "tags", []) or []),
            parent_id=task_id, verify=[test_id], proof_type="test",
            oracle=f"pytest {test_id} passes with rc 0.")
        children.append(child.id)
    action = "decomposed" if children else "blocked"
    reason = (f"step {step_id} did not advance after {STALL_ATTEMPTS} "
              f"attempts; ")
    if children:
        reason += f"split into children: {', '.join(children)}"
    elif _last_outcome_was_a_kill(task_svc, task_id, step_id):
        # HONESTY. exit=-9 is SIGKILL: the step ran out of wall clock (or
        # the host killed it), it did not fail to name a test. Epic
        # 9f60a849 reported "no red test id was named in the last proof"
        # three times for a step that was being killed at the 900 s bound,
        # which sends whoever reads it hunting for a test problem that does
        # not exist. Name the real thing, and name the knob that changes it.
        reason += (f"the step was KILLED before it reported (exit=-9, "
                   f"SIGKILL) -- it ran past its "
                   f"{int(_step_timeout_s(step_id))}s budget. Raise "
                   f"PRISM_TASK_RUNNER_STEP_TIMEOUT_S, or narrow what this "
                   f"step has to run")
    else:
        reason += "no red test id was named in the last proof"
    task_svc.update(task_id, status="blocked", blocked_reason=reason)
    task_svc.record_history(task_id, action="runner_stall",
                            details=reason, actor=SEAT_ID)
    return {"ok": True, "task_id": task_id, "step": step_id, "run_id": "",
            "tokens": 0, "cost_usd": 0.0, "report": None,
            "stalled": {"action": action, "children": children,
                        "reason": reason}}


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



def _claim_service(project: str):
    """The per-task lease, or None when it cannot be built.

    Returns None rather than raising: a seat that cannot reach the claims db
    must still be able to drive, exactly as it did before this wiring. The
    lease is a safety net, never a new single point of failure.
    """
    try:
        from prism_service.services.claim_service import ClaimService

        return ClaimService(db_path=_scores_db_for(project))
    except Exception:
        return None


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

    if _stall_count(task_svc, task_id, job["step"]) >= STALL_ATTEMPTS:
        return _handle_stall(task_svc, task_id, job["step"])

    ws = task_workspace.workspace_for(task_id) or {}
    work_dir = ws.get("path")
    if not work_dir:
        return {"ok": False, "task_id": task_id, "step": job["step"],
                "reason": "no workspace on file for task"}

    from prism_service.inference import claude_cli
    from prism_service.services import drive_heartbeat

    # ONE DRIVER PER TASK WORKTREE (task 1bcb2b24). On 2026-08-30 two
    # `claude -p` processes ran the same step against this same directory --
    # same index, same HEAD -- and a test file was overwritten mid-write while
    # an agent authored it. _foreign_driver_on above is not enough: it sees a
    # driver only if that driver posts a HEARTBEAT, and it checks once rather
    # than for the life of the run. ClaimService's partial unique index makes
    # a second claim fail closed inside sqlite, and reaps an expired lease so
    # a crashed holder cannot wedge the task.
    claim = _claim_service(project)
    claim_id = None
    if claim is not None:
        claim_id = claim.acquire(
            task_id, holder_id=SEAT_ID, ttl_s=_step_timeout_s(job["step"]))
        if claim_id is None:
            holder = claim.holder_of(task_id) or "another driver"
            # Losing the race is not a failure: somebody else is doing the
            # work. Skip without touching the task's step or status.
            return {"ok": False, "task_id": task_id, "step": job["step"],
                    "reason": f"already driving: held by {holder}"}

    scores_db = _scores_db_for(project)
    drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": task_id, "step": job["step"], "elapsed_s": 0,
        "last_tool": "claude_cli.invoke", "work_units": 1,
        "driver": RUNNER_DRIVER,
    })
    # A node's DECLARED plan governs this invoke (task 8848089d): the
    # codified sub-steps run here as Python at zero tokens, and the agentic
    # middle gets the declared model/turns/budget instead of the blanket
    # 30-turn / $2.00 default. `plan` is None for every step without a
    # credible declaration, and then nothing below changes behaviour.
    plan = _node_plan(project, job["step"])
    prompt = job["instructions"]
    run_id = str(uuid.uuid4())
    if plan and "premise-gather" in (plan.get("codified") or []):
        ctx = get_project(project)
        task = task_svc.get(task_id)
        preamble, facts = _codified_preamble(
            project, task, memory_svc=getattr(ctx, "memory_svc", None),
            task_svc=task_svc, brain_svc=getattr(ctx, "brain_svc", None))
        if preamble:
            prompt = preamble + "\n\n" + prompt
        _record_codified_run(
            project, task_id, "premise-gather", run_id, bool(facts),
            f"resolved {len(facts)} grounded facts")

    budget = _invoke_budget(job["step"], plan)
    try:
        result = claude_cli.invoke(
            prompt, work_dir=work_dir, plugin_dir=work_dir,
            allowed_tools=BUILD_TOOLS, project=project,
            purpose=f"task-runner@{job['step']}#{task_id[:8]}",
            session_id=str(uuid.uuid4()), **budget)
    except Exception as exc:
        if claim is not None:
            claim.release(claim_id)
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
    # The codified CHECK sub-step (task 8848089d): verify the report's own
    # shape with the SAME regexes the story_gate rubric uses, in-process and
    # at zero tokens, so a report that cites nothing is caught here rather
    # than one full gate sweep later. Advisory -- it records what it found
    # and never fails a step the model completed, because premise_grounded
    # remains the seat that actually decides.
    if plan and "premise-citation-check" in (plan.get("codified") or []) and proof:
        try:
            from prism_service.services import premise_gather

            verdict = premise_gather.citation_check(proof)
            _record_codified_run(
                project, task_id, "premise-citation-check", run_id,
                bool(verdict.get("ok")), str(verdict.get("reason") or ""))
        except Exception:
            pass

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

    if claim is not None:
        claim.release(claim_id)

    report = flow.flow_report(flow.Ident(
        task_id=task_id, session_id=SEAT_ID, outcome=outcome,
        expected_step=step_id, usage=usage,
        model=(usage or {}).get("model") or None), project=project)
    if not report.get("advanced", report.get("ok")):
        task_svc.record_history(
            task_id, action=ATTEMPT_ACTION, actor=SEAT_ID,
            details=f"step={step_id}; advanced=false; proof={proof[:2000]}")
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
