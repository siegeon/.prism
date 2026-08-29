"""The task runner drives several tasks per tick under the load breaker
(task 5c255196). `sweep_once()` drives up to PRISM_TASK_RUNNER_PARALLEL
eligible tasks per tick (default 1, the owner's cost guard), skips the
tick when the load breaker trips, and every started step still writes one
drive heartbeat row (the Live board source). `eligible_task`/`eligible_tasks`
are NEVER monkeypatched here: selection is the real code against real
in_progress rows (AC-6).
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


@pytest.fixture()
def make_project():
    """A fresh throwaway project with N in_progress tasks (workflow_step=""),
    per-task git workspaces torn down afterwards."""
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(n: int, gate_parked: int = 0):
        project = "parallel-ticks-" + uuid.uuid4().hex[:8]
        ctx = get_project(project)
        ids = []
        for i in range(n):
            t = ctx.task_svc.create(title=f"parallel task {i}")
            ctx.task_svc.update(t.id, status="in_progress")
            created.append(t.id)
            ids.append(t.id)
        parked = []
        for i in range(gate_parked):
            t = ctx.task_svc.create(title=f"parked task {i}")
            ctx.task_svc.update(t.id, status="in_progress",
                                workflow_step="story_gate")
            created.append(t.id)
            parked.append(t.id)
        return ctx, project, ids, parked

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


class _FakeResult:
    def __init__(self, text: str = "## Premises\n- ok - UNVERIFIED\n") -> None:
        self.text = text
        self.usage = None
        self.exit_code = 0
        self.run_id = "run-parallel"

    # The runner reads these on the real ClaudeCliResult (task_runner.py:442).
    def final_text(self) -> str:
        return self.text

    def graceful_budget_stop(self) -> bool:
        return False


class _Call:
    def __init__(self, purpose: str) -> None:
        self.purpose = purpose
        # purpose == "task-runner@<step>#<task8>" (task_runner._run_one_step)
        self.task_id = purpose.rsplit("#", 1)[-1]


def _arm(monkeypatch, project: str, calls: list):
    """Real selection, real _run_one_step, stubbed `claude -p`."""
    from prism_service import project_context
    from prism_service.inference import claude_cli
    from prism_service.services import task_runner as tr

    monkeypatch.setattr(project_context, "get_all_projects", lambda: [project])
    monkeypatch.setattr(tr, "_spend_ceiling_crossed", lambda: False)
    monkeypatch.setattr(tr, "_system_overloaded", lambda: False)
    monkeypatch.setattr(tr, "_rr_index", 0)

    def _invoke(prompt, **kw):
        calls.append(_Call(kw.get("purpose", "")))
        return _FakeResult()

    monkeypatch.setattr(claude_cli, "invoke", _invoke)
    return tr


def _started_ids(calls) -> set[str]:
    return {c.task_id for c in calls}


# AC-5 / AC-4 (parsing) -----------------------------------------------------

@pytest.mark.parametrize("raw", ["", "0", "-2", "abc"])
def test_parallel_env_parsing(monkeypatch, raw):
    from prism_service.services import task_runner as tr

    monkeypatch.setenv("PRISM_TASK_RUNNER_PARALLEL", raw)
    assert tr._parallel() == 1
    monkeypatch.setenv("PRISM_TASK_RUNNER_PARALLEL", "3")
    assert tr._parallel() == 3


def test_unset_parallel_is_one(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.delenv("PRISM_TASK_RUNNER_PARALLEL", raising=False)
    assert tr._parallel() == 1


# AC-1 ----------------------------------------------------------------------

def test_parallel_three_starts_three_of_four(make_project, monkeypatch):
    ctx, project, ids, _ = make_project(4)
    calls: list = []
    tr = _arm(monkeypatch, project, calls)
    monkeypatch.setenv("PRISM_TASK_RUNNER_PARALLEL", "3")

    res = tr.sweep_once()

    assert len(calls) == 3, [c.purpose for c in calls]
    assert isinstance(res, dict), "sweep_once keeps its dict return (AC-8)"
    assert len(res.get("started") or []) == 3
    untouched = [t for t in ids if t[:8] not in _started_ids(calls)]
    assert len(untouched) == 1, "exactly one task waits for the next tick"
    waiting = ctx.task_svc.get(untouched[0])
    assert (waiting.workflow_step or "") == "", "the waiting task did not move"


# AC-2 ----------------------------------------------------------------------

def test_started_steps_never_share_a_task_id(make_project, monkeypatch):
    _, project, _, _ = make_project(4)
    calls: list = []
    tr = _arm(monkeypatch, project, calls)
    monkeypatch.setenv("PRISM_TASK_RUNNER_PARALLEL", "3")

    tr.sweep_once()

    assert calls, "the tick started nothing"
    assert len(_started_ids(calls)) == len(calls), (
        "two started steps share a task id: " + str([c.purpose for c in calls]))


# AC-3 ----------------------------------------------------------------------

def test_tripped_breaker_starts_zero_steps_at_parallel_three(
        make_project, monkeypatch):
    _, project, _, _ = make_project(3)
    calls: list = []
    tr = _arm(monkeypatch, project, calls)
    monkeypatch.setenv("PRISM_TASK_RUNNER_PARALLEL", "3")
    monkeypatch.setattr(tr, "_system_overloaded", lambda: True)
    runs: list = []
    monkeypatch.setattr(tr, "run_one_step",
                        lambda pid, tid: runs.append(tid) or {"ok": True})

    assert tr.sweep_once() is None
    assert runs == [] and calls == []


# AC-4 ----------------------------------------------------------------------

def test_unset_parallel_starts_one_step(make_project, monkeypatch):
    _, project, _, _ = make_project(4)
    calls: list = []
    tr = _arm(monkeypatch, project, calls)
    monkeypatch.delenv("PRISM_TASK_RUNNER_PARALLEL", raising=False)

    res = tr.sweep_once()

    assert tr._parallel() == 1
    assert len(calls) == 1, [c.purpose for c in calls]
    assert isinstance(res, dict) and len(res.get("started") or []) == 1


# AC-6 ----------------------------------------------------------------------

def test_gate_parked_task_is_not_selected_for_a_slot(make_project, monkeypatch):
    _, project, ids, parked = make_project(2, gate_parked=1)
    calls: list = []
    tr = _arm(monkeypatch, project, calls)
    monkeypatch.setenv("PRISM_TASK_RUNNER_PARALLEL", "3")

    tr.sweep_once()

    started = _started_ids(calls)
    assert parked[0][:8] not in started
    assert started == {t[:8] for t in ids}
    assert tr.eligible_tasks(project, 3) == [], "all agent-step tasks consumed"


# AC-7 ----------------------------------------------------------------------

def test_each_started_step_records_a_heartbeat(make_project, monkeypatch):
    from prism_service.services import drive_heartbeat

    _, project, _, _ = make_project(4)
    calls: list = []
    tr = _arm(monkeypatch, project, calls)
    monkeypatch.setenv("PRISM_TASK_RUNNER_PARALLEL", "3")
    beats: list = []
    monkeypatch.setattr(drive_heartbeat, "record_heartbeat",
                        lambda db, row: beats.append(dict(row)) or {"ok": True})

    tr.sweep_once()

    assert len(beats) == 3
    assert {b["task_id"][:8] for b in beats} == _started_ids(calls)
