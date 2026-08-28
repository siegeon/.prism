"""task_runner stops looping when a step cannot advance (task 82cc05ee).

Trace: on 3a3f90da the runner re-spawned the same implement_tasks step 6+
times over 4 hours; each fresh agent found the same 4 pinned tests red and
reported it honestly. `_run_one_step` (task_runner.py) keeps no attempt
memory and no code path counts repeated no-advance reports on one step.

These tests pin the repair. Count source is durable task history, never a
module dict. After STALL_ATTEMPTS (3) non-advancing reports on one step the
fourth tick must NOT invoke claude; it decomposes into one child per red
test id named in the last proof, or blocks with a blocked_reason naming the
step when no id is named. AC ids follow the task's plan_doc.
"""
from __future__ import annotations

import importlib
import re
import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

STEP = "implement_tasks"
RED_IDS = [
    "tests/unit/test_alpha.py::test_one",
    "tests/unit/test_alpha.py::test_two",
    "tests/unit/test_beta.py::test_three",
    "tests/unit/test_beta.py::test_four",
]
RED_PROOF = (
    "Ran the pinned suite. Still red:\n"
    + "\n".join(f"FAILED {i}" for i in RED_IDS)
    + "\n4 failed, 3 passed.\n"
)
PROSE_PROOF = "The build could not advance. The fixture never loads.\n"


class _FakeResult:
    def __init__(self, text: str) -> None:
        self._text = text
        self.exit_code = 0
        self.run_id = "run-stall"
        self.usage = {}

    def final_text(self) -> str:
        return self._text


@pytest.fixture()
def stalled(monkeypatch, tmp_path):
    """A real TaskService task at implement_tasks whose every report fails
    to advance (mirrors conductor_flow.py:613 flow_report_failure)."""
    from prism_service.project_context import get_project
    from prism_service.api import conductor_flow as flow
    from prism_service.inference import claude_cli
    from prism_service.services import task_workspace

    project = "stall-" + uuid.uuid4().hex[:8]
    ctx = get_project(project)
    task = ctx.task_svc.create(title="stalled task", tags=["x"], priority=5)
    ctx.task_svc.update(task.id, status="in_progress", workflow_step=STEP)

    invoke_calls: list[dict] = []
    state = {"proof": RED_PROOF}

    def _invoke(prompt, **kw):
        invoke_calls.append({"prompt": prompt, **kw})
        return _FakeResult(state["proof"])

    def _flow_start(ident, project=None):
        return {"ok": True, "job": {"step": STEP, "kind": "agent",
                                    "instructions": "make tests green"}}

    def _flow_report(ident, project=None):
        ctx.task_svc.record_history(
            ident.task_id, action="flow_report_failure",
            details=f"step={STEP}; outcome=pass", actor=ident.session_id)
        return {"ok": False, "step": STEP, "advanced": False}

    monkeypatch.setattr(claude_cli, "invoke", _invoke)
    monkeypatch.setattr(flow, "flow_start", _flow_start)
    monkeypatch.setattr(flow, "flow_report", _flow_report)
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"path": str(tmp_path)})
    return {"ctx": ctx, "project": project, "task_id": task.id,
            "invoke_calls": invoke_calls, "state": state}


def _tick(s):
    from prism_service.services import task_runner as tr
    return tr.run_one_step(s["project"], s["task_id"])


# AC-1 -- the fourth tick does not spawn an identical attempt
def test_fourth_attempt_is_not_spawned(stalled):
    results = [_tick(stalled) for _ in range(4)]
    assert len(stalled["invoke_calls"]) == 3, (
        "three non-advancing reports on one step must stop the fourth "
        f"invoke; got {len(stalled['invoke_calls'])} invokes: {results[-1]}")


# AC-2 -- the count lives in task history, not process memory
def test_stall_count_survives_module_reload(stalled):
    from prism_service.services import task_runner as tr
    for _ in range(3):
        _tick(stalled)
    importlib.reload(tr)
    _tick(stalled)
    assert len(stalled["invoke_calls"]) == 3
    actions = [h.action for h in stalled["ctx"].task_svc.history(
        stalled["task_id"])]
    assert actions.count("runner_attempt") >= 3, actions


# AC-3 / AC-4 -- red test ids decompose into children; parent blocks
def test_stalled_step_decomposes_into_children(stalled):
    for _ in range(4):
        _tick(stalled)
    svc = stalled["ctx"].task_svc
    children = svc.list(parent_id=stalled["task_id"])
    assert len(children) == 4, [c.title for c in children]
    verifies = sorted(v for c in children for v in c.verify)
    assert verifies == sorted(RED_IDS)
    for c in children:
        assert c.parent_id == stalled["task_id"]
        assert c.oracle != ""
        assert len(c.verify) == 1
    parent = svc.get(stalled["task_id"])
    assert parent.status == "blocked", parent.status
    assert STEP in parent.blocked_reason
    for c in children:
        assert c.id in parent.blocked_reason


# AC-5 -- no test id in the proof: block with reason, no child
def test_stall_without_test_ids_blocks_with_reason(stalled):
    stalled["state"]["proof"] = PROSE_PROOF
    for _ in range(4):
        _tick(stalled)
    svc = stalled["ctx"].task_svc
    parent = svc.get(stalled["task_id"])
    assert parent.status == "blocked", parent.status
    assert STEP in parent.blocked_reason
    assert svc.list(parent_id=stalled["task_id"]) == []


# AC-6 -- a blocked parent is not driven again
def test_blocked_parent_is_not_eligible(stalled):
    from prism_service.services import task_runner as tr
    for _ in range(4):
        _tick(stalled)
    assert tr.eligible_task(stalled["project"]) is None


# AC-8 -- the stalled tick keeps every existing return key
def test_stalled_tick_return_shape(stalled):
    for _ in range(3):
        _tick(stalled)
    res = _tick(stalled)
    assert res["ok"] is True, res
    assert res["task_id"] == stalled["task_id"]
    assert res["step"] == STEP
    assert "run_id" in res
    assert res["stalled"]["action"] in {"decomposed", "blocked"}, res


# AC-7 -- the task page renders children and blocked_reason (no JS runner;
# pin the RENDERED tag, not a comment)
def test_task_page_renders_children_and_blocked_reason():
    src = (_SERVICE_ROOT / "prism_service/web/src/pages/TaskDetailPage.tsx"
           ).read_text(encoding="utf-8")
    assert re.search(r"<LinkedText text=\{task\.blocked_reason\}", src)
    assert re.search(r"/api/tasks\?[^`]*parent_id=\$\{id\}", src)
