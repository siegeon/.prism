"""A red green receipt rewinds the drive to implement_tasks (task ad92c0e9).

Non-policy consumer of oracle_spec: when the LATEST EvidenceReceipt for a
task parked at green_gate is FAILED and FRESH (minted at the workspace's
current tree), the drive goes back to implement_tasks with the failing
test ids named, instead of parking for a human. Each rewind writes ONE
audited history row (action=rewind). A per-project budget
(.prism/behaviors/conductor.json -> rewind_budget, default 3) caps the
loop; the next red receipt past the budget parks with a gate_reason that
names the budget and the failing tests.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from prism_service.services import oracle_spec

DEFAULT_REWIND_BUDGET = 3
REWIND_TO_STEP = "implement_tasks"
REWIND_ACTOR = "conductor-adjudicator"
REWIND_ACTION = "rewind"


def _source_path(project: str) -> str:
    """Repo root whose .prism/behaviors/conductor.json holds the budget."""
    return os.environ.get("PRISM_SOURCE_PATH") or os.getcwd()


def rewind_budget(project: str) -> int:
    path = os.path.join(_source_path(project), ".prism", "behaviors",
                        "conductor.json")
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh).get("rewind_budget")
        return int(value) if value is not None else DEFAULT_REWIND_BUDGET
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_REWIND_BUDGET


def _workspace_path(task) -> str:
    """The task's workspace checkout, WITHOUT creating one."""
    ws = getattr(task, "workspace", "") or ""
    if ws:
        return str(ws)
    try:
        from prism_service.services import task_workspace
        fn = getattr(task_workspace, "workspace_path", None)
        return str(fn(task.id)) if fn else ""
    except Exception:  # noqa: BLE001 - best effort, receipt tree decides
        return ""


def failing_tests(receipt) -> list[str]:
    return [str(o.get("name") or "")
            for o in (getattr(receipt, "observations", None) or [])
            if isinstance(o, dict) and not o.get("passed")]


def rewind_count(task_svc, task_id: str) -> int:
    return sum(1 for h in task_svc.history(task_id)
               if h.action == REWIND_ACTION)


def maybe_rewind(ctx, task, project: str) -> Optional[dict]:
    """Rewind a task at a PENDING green_gate on a fresh FAILED receipt.

    Returns None when nothing applies (other step, passed gate, missing or
    passed receipt, stale receipt), {"ok": True, ...} on a rewind, and
    {"ok": False, "parked": True, ...} when the rewind budget is spent.
    """
    if getattr(task, "workflow_step", "") != "green_gate":
        return None
    if getattr(task, "gate_state", "") != "pending":
        return None
    receipt = oracle_spec.latest_receipt(project, task.id)
    if receipt is None or receipt.passed:
        return None
    tree = oracle_spec.current_tree_sha(_workspace_path(task))
    if not tree or receipt.tree_sha != tree:
        return None
    task_svc = ctx.task_svc
    failing = failing_tests(receipt)
    names = ", ".join(failing) or "(no failing test ids in the receipt)"
    budget = rewind_budget(project)
    spent = rewind_count(task_svc, task.id)
    if spent >= budget:
        reason = (f"Rewind budget {budget} spent at tree {tree}; "
                  f"still failing: {names}")
        task_svc.update(task.id, gate_reason=reason)
        return {"ok": False, "parked": True, "task_id": task.id,
                "budget": budget, "failing": failing, "reason": reason}
    attempt = spent + 1
    reason = (f"Rewind {attempt}/{budget}: green receipt FAILED at tree "
              f"{tree}; failing: {names}")
    task_svc.update(task.id, workflow_step=REWIND_TO_STEP,
                    gate_state="pending", gate_reason=reason)
    task_svc.record_history(
        task.id, action=REWIND_ACTION,
        details=f"green_gate -> {REWIND_TO_STEP}; attempt={attempt}; "
                f"tree={tree}; failing={names}",
        actor=REWIND_ACTOR)
    return {"ok": True, "task_id": task.id, "from_step": "green_gate",
            "to_step": REWIND_TO_STEP, "attempt": attempt,
            "budget": budget, "failing": failing}
