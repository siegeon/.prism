"""Task Score: delivered size against inference effort, minus rework.

PRISM records every commit, token and gate decision for a task and gives
it no number. This module is that number.

    score = multiplier * throughput / (1 + rework)
    throughput = log10(1 + size) / (effort_tokens / 1000)

WHERE THE EFFORT TERM COMES FROM, and where it must never come from.
`agent_runs` carries a real task_id (agent_runs_data.py:55) and a real
`tokens` column (:65), so two tasks worked by one session report two
different costs. The task-level session rollup does NOT: it joins a table
keyed by session alone (engines/brain_engine.py:1322 declares
`session_id TEXT PRIMARY KEY`), so it hands every task that shared a
session the same lifetime total. Measured live on 2026-09-08, task
4e6e7417 read 8448748766 tokens and task 7a72ebcb read 8462721950 -- two
unrelated tasks 0.16 percent apart -- while a task that really ran read 0.
A ratio built on that is meaningless, so this module reads agent_runs and
tests/unit/test_task_score.py refuses the other path by name.

SIZE is capped by a logarithm on purpose. Raw churn rewards bulk: over the
77 tasks that hold both shipped commits and measured tokens, throughput
ran 0.7 to 304 lines for each 1k tokens (median 8.8), and the top of that
range was a 2000 line change. log10 keeps a bigger change ahead of a
smaller one without letting it run away.

Pure data access, in the manner of agent_runs_data.py: sqlite, git and the
standard library only.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Any, Optional

# Owner rubric, recorded as memory mx-b0703f: to codify a node scores
# highest, agentic inference scores less, and hand work scores negative.
# REPORTED, never enforced -- no gate reads this, because a multiplier a
# gate trusted could be moved by editing a tag.
RESOLUTION_MULTIPLIERS: dict[str, float] = {
    "codify": 1.5,
    "agentic": 1.0,
    "hand": -1.0,
}
DEFAULT_MULTIPLIER = 1.0
BASE_REF = "origin/main"


def _field(task: Any, name: str, default: Any) -> Any:
    """Read `name` off a task that may be a dict or a model object."""
    if isinstance(task, dict):
        return task.get(name, default)
    return getattr(task, name, default)


def delivered_size(repo_root: str, task_id: str,
                   base_ref: str = BASE_REF) -> int:
    """Added plus deleted lines over commits trailered for `task_id` that
    are REACHABLE FROM `base_ref`.

    Reachability is the whole point: done means shipped, so work sitting on
    an unmerged branch measures zero however large it is.
    """
    short = str(task_id)[:8]
    if not short or not repo_root:
        return 0
    try:
        out = subprocess.run(
            ["git", "log", base_ref, "--numstat", "--format=",
             "--grep", f"[task:{short}", "--fixed-strings"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if out.returncode != 0:
        return 0
    total = 0
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            total += int(parts[0]) + int(parts[1])
    return total


def effort_tokens(scores_db: str, task_id: str) -> int:
    """Sum of agent_runs.tokens for THIS task id, and nothing else."""
    from prism_service.services.agent_runs_data import get_agent_runs

    rows = get_agent_runs(scores_db, limit=100_000, task_id=task_id)
    return sum(int(r.get("tokens") or 0) for r in rows)


def rework_points(history: Optional[list]) -> float:
    """PSP defect drag, read off rows the conductor already writes.

    A park is a whole point: the task stopped and waited for a person. A
    gate reject is a whole point: the work was returned. A retry dispatch
    is a QUARTER, and only when no later advance_task follows it -- a
    dispatch that moved the task bought progress and is not drag. Task
    338f7810 is the case this term exists to rank: 37 dispatches over
    4h40m, advancing and rewinding the whole time.
    """
    rows = list(history or [])
    points = 0.0
    for i, row in enumerate(rows):
        action = str(_field(row, "action", "") or "")
        details = str(_field(row, "details", "") or "").lower()
        if action == "resume_actuator_parked":
            points += 1.0
        elif action == "gate_decide" and "reject" in details:
            points += 1.0
        elif action == "resume_actuator_dispatch":
            later = rows[i + 1:]
            advanced = any(
                str(_field(r, "action", "") or "") == "advance_task"
                for r in later)
            if not advanced:
                points += 0.25
    return points


def resolution_of(task: Any) -> tuple[str, float]:
    """The recorded resolution class and its multiplier, from a
    `resolution:<class>` tag. Unset reads as unset, never as hand work."""
    for tag in (_field(task, "tags", None) or []):
        text = str(tag)
        if text.startswith("resolution:"):
            name = text.split(":", 1)[1].strip().lower()
            if name in RESOLUTION_MULTIPLIERS:
                return name, RESOLUTION_MULTIPLIERS[name]
    return "unset", DEFAULT_MULTIPLIER


# One point of drag each. A resume dispatch is weighed separately, below.
_FULL_REWORK_ACTIONS = ("resume_actuator_parked", "rewind")
_DISPATCH_ACTION = "resume_actuator_dispatch"
_ADVANCE_ACTION = "advance_task"
_BARREN_DISPATCH_WEIGHT = 0.25


def rework_points(history: list) -> float:
    """Drag recorded in task_history. A park, a rewind or a rejected gate is
    a whole point. A resume dispatch counts a quarter ONLY when no later
    advance_task follows it -- a dispatch that moved the task bought
    progress and is not drag. Task 338f7810, 37 dispatches over 4h40m, is
    the oscillator this term exists to rank.
    """
    rows = [r for r in (history or []) if isinstance(r, dict)]
    rows.sort(key=lambda r: str(r.get("timestamp") or ""))
    later_advance = False
    points = 0.0
    # Walk backwards, so "did an advance follow this dispatch?" is one flag
    # rather than a rescan of the tail per dispatch row.
    for row in reversed(rows):
        action = str(row.get("action") or "")
        details = str(row.get("details") or "")
        if action == _ADVANCE_ACTION:
            later_advance = True
        elif action == _DISPATCH_ACTION:
            if not later_advance:
                points += _BARREN_DISPATCH_WEIGHT
        elif action in _FULL_REWORK_ACTIONS or "rewind" in action:
            points += 1.0
        elif action == "gate_decide" and "reject" in details.lower():
            points += 1.0
    return points


def resolution_of(task: Any) -> str:
    """The `resolution:<kind>` tag, or "unset". Advisory only."""
    for tag in _field(task, "tags", None) or []:
        text = str(tag)
        if text.startswith("resolution:"):
            kind = text.split(":", 1)[1].strip().lower()
            if kind in RESOLUTION_MULTIPLIERS:
                return kind
    return "unset"


def score_task(repo_root: str, scores_db: str, task: Any,
               history: list) -> dict:
    """The whole score for one task, with every input it was built from.

    Never raises on a missing input: work that has not landed scores 0.0 and
    the reason names origin/main, and a task with no measured tokens scores
    None with a stated reason rather than dividing by zero.
    """
    task_id = str(_field(task, "id", "") or "")
    resolution = resolution_of(task)
    multiplier = RESOLUTION_MULTIPLIERS.get(resolution, DEFAULT_MULTIPLIER)

    size = delivered_size(repo_root, task_id)
    tokens = effort_tokens(scores_db, task_id)
    rework = rework_points(history)
    drag = 1.0 + rework

    out: dict = {
        "task_id": task_id,
        "size": size,
        "effort_tokens": tokens,
        "rework": rework,
        "rework_drag": drag,
        "throughput": 0.0,
        "lines_per_1k": 0.0,
        "score": None,
        "resolution": resolution,
        "multiplier": multiplier,
        "advisory": True,
        "reason": "",
    }

    if size <= 0:
        out["score"] = 0.0
        out["reason"] = (
            f"no commit trailered for this task is reachable from {BASE_REF}: "
            "the work is not shipped, so it scores zero")
        return out

    if tokens <= 0:
        out["reason"] = (
            "no measured tokens in agent_runs for this task, so a cost ratio "
            "would be a number that nothing backs")
        return out

    per_1k = tokens / 1000.0
    throughput = math.log10(1 + size) / per_1k
    out["throughput"] = throughput
    out["lines_per_1k"] = size / per_1k
    out["score"] = throughput / drag
    out["reason"] = (
        f"{size} shipped lines over {tokens} agent tokens, divided by "
        f"{drag:.2f} of rework drag")
    return out


def score_task(repo_root: str, scores_db: str, task: Any,
               history: Optional[list] = None,
               base_ref: str = BASE_REF) -> dict:
    """Score one task. Never raises, and never invents a number.

    Two cases report no score rather than a misleading one:
      * nothing reachable from `base_ref` -> score 0.0, because done means
        shipped and unlanded work has delivered nothing yet;
      * no measured tokens -> score None, because a ratio with no
        denominator is not zero, it is unknown.
    """
    task_id = str(_field(task, "id", "") or "")
    size = delivered_size(repo_root, task_id, base_ref)
    tokens = effort_tokens(scores_db, task_id)
    rework = rework_points(history)
    resolution, multiplier = resolution_of(task)
    shipped = size > 0

    out: dict[str, Any] = {
        "task_id": task_id,
        "size": size,
        "effort_tokens": tokens,
        "rework": rework,
        "resolution": resolution,
        "multiplier": multiplier,
        "advisory": True,
        "shipped": shipped,
        "throughput": None,
        "lines_per_1k": None,
        "score": None,
        "reason": "",
    }

    if not shipped:
        out["score"] = 0.0
        out["reason"] = (
            f"no commit trailered [task:{task_id[:8]}] is reachable from "
            f"{base_ref}, so nothing has been delivered yet")
        return out

    if tokens <= 0:
        out["reason"] = (
            "no measured tokens on agent_runs for this task, so cost for "
            "each delivered line cannot be computed")
        return out

    per_1k = tokens / 1000.0
    out["throughput"] = math.log10(1 + size) / per_1k
    out["lines_per_1k"] = size / per_1k
    out["score"] = multiplier * out["throughput"] / (1.0 + rework)
    out["reason"] = (
        f"{size} delivered lines against {tokens} tokens, "
        f"{rework} rework")
    return out
