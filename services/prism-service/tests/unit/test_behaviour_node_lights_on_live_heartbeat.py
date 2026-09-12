"""Task b490fabc: a behaviour node lights up WHILE a long agentic dispatch
is still in flight, not just after it finishes.

_conductor_behavior_workflows' occupancy fix (test_behaviour_substep_
activity.py) only reads node_recent_runs, a scores.db lookback over rows
written when a route's HTTP call RETURNS. A single call that stays open for
90+ minutes (implement-tasks-loop's reason-loop, one real drive: 108 turns,
no completed row yet) has nothing there to find, so the node painted "000"
for the node's entire true duration (owner, live: "i stillcan not see the
ending working in real time").

get_workflows must ALSO light the behaviour's entry node from the SAME
drive heartbeat /api/conductor/state's own "driving" badge already trusts,
for any live (non-stale) task parked at the FSM step this behaviour answers
for (_STEP_FOR_BEHAVIOUR) -- independent of whether scores.db has ever
recorded a completed run for it.
"""
from __future__ import annotations

import types

import pytest


def _mk_task(**over):
    from prism_service.models.task import Task

    base = dict(
        id="t-1", title="A task", description="", status="in_progress",
        priority=5, assigned_agent="", updated_at="2026-09-12T00:00:00Z",
        workflow_step="implement_tasks", gate_state="none", parent_id="",
        tags=[],
    )
    base.update(over)
    return Task(**base)


class _Svc:
    def __init__(self, tasks):
        self.tasks = list(tasks)

    def list(self, status=None, assigned_agent=None, tag=None,
             story_file=None, parent_id=None, id=None):
        return list(self.tasks)


def _behavior_entry():
    return {
        "id": "implement-tasks-loop",
        "name": "Implement tasks (Observe-Reason)",
        "description": "Runs on the 'pipeline' fsm.",
        "steps": [
            {"id": "loop", "agent": "conductor", "type": "behavior",
             "agentic": False, "route": "reason-loop"},
            {"id": "text-challenge", "agent": "conductor", "type": "behavior",
             "agentic": False, "route": "text-challenge",
             "depends_on": ["loop"]},
        ],
        "bots": [],
        "occupancy": {"loop": 0, "text-challenge": 0},
    }


def _behavior_entry_2():
    """A second, unrelated behaviour -- its FSM step (draft_story) has no
    active task in the tests that use it, so it must stay idle while
    proving the heartbeat lookup still ran only once overall."""
    return {
        "id": "draft-story-loop",
        "name": "Draft story",
        "description": "Runs on the 'pipeline' fsm.",
        "steps": [
            {"id": "gather", "agent": "conductor", "type": "behavior",
             "agentic": False, "route": "premise-gather"},
        ],
        "bots": [],
        "occupancy": {"gather": 0},
    }


def _scripted_validation(project="prism"):
    return {
        "id": "validation", "name": "Build and test", "description": "v",
        "project_type": "python", "steps": [], "bots": [], "occupancy": {},
    }


def _wire(monkeypatch, svc, data_dir, entries=None):
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(
        workflows_api, "get_project",
        lambda p: types.SimpleNamespace(task_svc=svc, _data_dir=data_dir))
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)
    _entries = entries if entries is not None else [_behavior_entry()]
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows",
                        lambda project: _entries)
    return workflows_api


def _entry(result, entry_id):
    for w in result["workflows"]:
        if w["id"] == entry_id:
            return w
    raise AssertionError(f"{entry_id} not in {[w['id'] for w in result['workflows']]}")


def test_a_live_in_flight_dispatch_lights_the_entry_node(tmp_path, monkeypatch):
    """The exact live case: no completed run on file, a heartbeat updated
    seconds ago for the task standing at implement_tasks."""
    from prism_service.services import drive_heartbeat

    svc = _Svc([_mk_task(workflow_step="implement_tasks")])
    workflows_api = _wire(monkeypatch, svc, tmp_path)
    drive_heartbeat.record_heartbeat(str(tmp_path / "scores.db"), {
        "task_id": "t-1", "step": "implement_tasks", "elapsed_s": 5400,
        "last_tool": "dispatch_guard_live", "work_units": 108,
        "driver": "prism-task-runner",
    })

    result = workflows_api.get_workflows(project="prism")

    entry = _entry(result, "implement-tasks-loop")
    assert entry["occupancy"]["loop"] == 1, entry["occupancy"]


def test_no_heartbeat_at_all_leaves_the_node_idle(tmp_path, monkeypatch):
    """Same task, same step, but nobody ever beat -- must stay honest."""
    svc = _Svc([_mk_task(workflow_step="implement_tasks")])
    workflows_api = _wire(monkeypatch, svc, tmp_path)

    result = workflows_api.get_workflows(project="prism")

    entry = _entry(result, "implement-tasks-loop")
    assert entry["occupancy"]["loop"] == 0, entry["occupancy"]


def test_a_stale_heartbeat_does_not_count_as_driving(tmp_path, monkeypatch):
    from prism_service.services import drive_heartbeat

    svc = _Svc([_mk_task(workflow_step="implement_tasks")])
    workflows_api = _wire(monkeypatch, svc, tmp_path)
    drive_heartbeat.record_heartbeat(str(tmp_path / "scores.db"), {
        "task_id": "t-1", "step": "implement_tasks", "elapsed_s": 5400,
        "last_tool": "dispatch_guard_live", "work_units": 108,
        "driver": "prism-task-runner",
    })
    conn = drive_heartbeat._connect(str(tmp_path / "scores.db"))
    conn.execute(
        "UPDATE drive_heartbeats SET last_progress_at = ? WHERE task_id = ?",
        ("2000-01-01T00:00:00+00:00", "t-1"))
    conn.commit()
    conn.close()

    result = workflows_api.get_workflows(project="prism")

    entry = _entry(result, "implement-tasks-loop")
    assert entry["occupancy"]["loop"] == 0, entry["occupancy"]


def test_a_heartbeat_for_a_different_step_does_not_light_this_node(tmp_path, monkeypatch):
    """The task is alive right now, but on draft_story -- not the FSM step
    implement-tasks-loop answers for. Only its own step's live task may
    light it."""
    from prism_service.services import drive_heartbeat

    svc = _Svc([_mk_task(workflow_step="draft_story")])
    workflows_api = _wire(monkeypatch, svc, tmp_path)
    drive_heartbeat.record_heartbeat(str(tmp_path / "scores.db"), {
        "task_id": "t-1", "step": "draft_story", "elapsed_s": 30,
        "last_tool": "some-tool", "work_units": 1, "driver": "prism-task-runner",
    })

    result = workflows_api.get_workflows(project="prism")

    entry = _entry(result, "implement-tasks-loop")
    assert entry["occupancy"]["loop"] == 0, entry["occupancy"]


def test_the_heartbeat_lookup_runs_once_regardless_of_entry_count(tmp_path, monkeypatch):
    """The first version of this fix called drive_heartbeat.latest() once
    per behaviour entry per candidate task -- measured >90s (still not
    returned) on a live, write-contended instance. get_workflows must call
    the batched latest_many exactly once per request, however many
    behaviour entries are in the catalog."""
    from prism_service.services import drive_heartbeat

    svc = _Svc([_mk_task(workflow_step="implement_tasks")])
    workflows_api = _wire(
        monkeypatch, svc, tmp_path,
        entries=[_behavior_entry(), _behavior_entry_2()])
    drive_heartbeat.record_heartbeat(str(tmp_path / "scores.db"), {
        "task_id": "t-1", "step": "implement_tasks", "elapsed_s": 5400,
        "last_tool": "dispatch_guard_live", "work_units": 108,
        "driver": "prism-task-runner",
    })
    calls = []
    real_latest_many = drive_heartbeat.latest_many

    def _counting(scores_db, task_ids):
        calls.append(list(task_ids))
        return real_latest_many(scores_db, task_ids)

    monkeypatch.setattr(drive_heartbeat, "latest_many", _counting)

    result = workflows_api.get_workflows(project="prism")

    assert len(calls) == 1, calls
    assert _entry(result, "implement-tasks-loop")["occupancy"]["loop"] == 1
    assert _entry(result, "draft-story-loop")["occupancy"]["gather"] == 0


def test_a_done_task_never_lights_the_node_even_with_a_fresh_heartbeat(tmp_path, monkeypatch):
    from prism_service.services import drive_heartbeat

    svc = _Svc([_mk_task(workflow_step="implement_tasks", status="done")])
    workflows_api = _wire(monkeypatch, svc, tmp_path)
    drive_heartbeat.record_heartbeat(str(tmp_path / "scores.db"), {
        "task_id": "t-1", "step": "implement_tasks", "elapsed_s": 5400,
        "last_tool": "dispatch_guard_live", "work_units": 108,
        "driver": "prism-task-runner",
    })

    result = workflows_api.get_workflows(project="prism")

    entry = _entry(result, "implement-tasks-loop")
    assert entry["occupancy"]["loop"] == 0, entry["occupancy"]
