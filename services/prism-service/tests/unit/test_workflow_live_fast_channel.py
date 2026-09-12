"""Task: "Nodes should be very fast linked to view" -- the 1-second live
channel behind GET /api/workflows/live (services/workflow_live.py).

GET /api/workflows answers the FULL catalog (engine JSON, node trend, role
bots, tiers) and measured 21-52s under load, so the canvas lagged a node's
real position by 20-60s. The live signal underneath it is tiny:
drive_heartbeat.latest_many joined to each active task's workflow_step,
mapped to a behaviour entry, lit on the step whose route matches the beat's
node. This module answers ONLY that, with exactly one latest_many call and
one task listing per call -- see test_behaviour_node_lights_on_live_heartbeat
.py for the same discipline get_workflows had to learn the hard way.
"""
from __future__ import annotations

import json
import types

from prism_service.services import drive_heartbeat, sqlite_db, workflow_live


def _write_behaviours(root):
    behaviours_dir = root / ".prism" / "behaviors" / "conductor"
    behaviours_dir.mkdir(parents=True)
    (behaviours_dir / "bot.json").write_text(json.dumps({
        "id": "conductor",
        "fsms": [{"fsmId": "pipeline",
                  "behaviorIds": ["implement-tasks-loop", "draft-story-loop"]}],
    }), encoding="utf-8")
    (behaviours_dir / "implement-tasks-loop.json").write_text(json.dumps({
        "id": "implement-tasks-loop",
        "steps": [
            {"id": "loop", "kind": "http-callback",
             "url": "${prismBackendUrl}/api/workflows/steps/reason-loop?project=${project}"},
            {"id": "text-challenge", "kind": "http-callback",
             "url": "${prismBackendUrl}/api/workflows/steps/text-challenge?project=${project}"},
        ],
    }), encoding="utf-8")
    (behaviours_dir / "draft-story-loop.json").write_text(json.dumps({
        "id": "draft-story-loop",
        "steps": [
            {"id": "gather", "kind": "http-callback",
             "url": "${prismBackendUrl}/api/workflows/steps/premise-gather?project=${project}"},
        ],
    }), encoding="utf-8")


def _setup(tmp_path, monkeypatch):
    from prism_service.services.task_service import TaskService

    root = tmp_path / "repo"
    root.mkdir()
    _write_behaviours(root)

    svc = TaskService(str(tmp_path / "tasks.db"), project="test")
    proj = types.SimpleNamespace(task_svc=svc, _data_dir=tmp_path)

    monkeypatch.setattr(workflow_live, "get_project", lambda p: proj)
    import prism_service.services.claude_transcripts as ct
    monkeypatch.setattr(ct, "_project_source_path", lambda p: str(root))
    return svc


def _beat(tmp_path, task_id, step, node="", tool="dispatch_guard_live",
          work_units=1, driver="prism-task-runner"):
    drive_heartbeat.record_heartbeat(str(tmp_path / "scores.db"), {
        "task_id": task_id, "step": step, "elapsed_s": 5,
        "last_tool": tool, "work_units": work_units,
        "driver": driver, "node": node,
    })


def _stale_it(tmp_path, task_id):
    conn = drive_heartbeat._connect(str(tmp_path / "scores.db"))
    conn.execute(
        "UPDATE drive_heartbeats SET last_progress_at = ? WHERE task_id = ?",
        ("2000-01-01T00:00:00+00:00", task_id))
    conn.commit()
    conn.close()


def test_a_fresh_beat_lights_the_matching_route(tmp_path, monkeypatch):
    svc = _setup(tmp_path, monkeypatch)
    task = svc.create("A task")
    svc.update(task.id, workflow_step="implement_tasks", status="in_progress")
    _beat(tmp_path, task.id, "implement_tasks", node="text-challenge",
          tool="node:text-challenge")

    result = workflow_live.live_for_project("test")

    entry = result["entries"]["implement-tasks-loop"]
    assert entry["occupancy"]["text-challenge"] == 1, entry["occupancy"]
    assert entry["occupancy"]["loop"] == 0, entry["occupancy"]
    assert entry["live"]["text-challenge"]["task_id"] == task.id
    assert entry["live"]["text-challenge"]["node"] == "text-challenge"


def test_an_empty_node_lights_the_entry_step(tmp_path, monkeypatch):
    svc = _setup(tmp_path, monkeypatch)
    task = svc.create("A task")
    svc.update(task.id, workflow_step="implement_tasks", status="in_progress")
    _beat(tmp_path, task.id, "implement_tasks", node="")

    result = workflow_live.live_for_project("test")

    entry = result["entries"]["implement-tasks-loop"]
    assert entry["occupancy"]["loop"] == 1, entry["occupancy"]
    assert entry["occupancy"]["text-challenge"] == 0, entry["occupancy"]


def test_a_stale_beat_lights_nothing(tmp_path, monkeypatch):
    svc = _setup(tmp_path, monkeypatch)
    task = svc.create("A task")
    svc.update(task.id, workflow_step="implement_tasks", status="in_progress")
    _beat(tmp_path, task.id, "implement_tasks", node="text-challenge")
    _stale_it(tmp_path, task.id)

    result = workflow_live.live_for_project("test")

    entry = result["entries"]["implement-tasks-loop"]
    assert entry["occupancy"]["loop"] == 0, entry["occupancy"]
    assert entry["occupancy"]["text-challenge"] == 0, entry["occupancy"]
    assert entry["live"] == {}


def test_dispatching_is_true_for_an_open_dispatch_tool(tmp_path, monkeypatch):
    svc = _setup(tmp_path, monkeypatch)
    task = svc.create("A task")
    svc.update(task.id, workflow_step="implement_tasks", status="in_progress")
    _beat(tmp_path, task.id, "implement_tasks", node="", tool="dispatch_guard_live")

    result = workflow_live.live_for_project("test")

    live_node = result["entries"]["implement-tasks-loop"]["live"]["loop"]
    assert live_node["dispatching"] is True
    assert live_node["tool"] == "dispatch_guard_live"


def test_dispatching_is_false_for_a_codified_tool(tmp_path, monkeypatch):
    svc = _setup(tmp_path, monkeypatch)
    task = svc.create("A task")
    svc.update(task.id, workflow_step="implement_tasks", status="in_progress")
    _beat(tmp_path, task.id, "implement_tasks", node="", tool="node_cleared")

    result = workflow_live.live_for_project("test")

    live_node = result["entries"]["implement-tasks-loop"]["live"]["loop"]
    assert live_node["dispatching"] is False


def test_conductor_occupancy_counts_non_terminal_tasks_per_step(tmp_path, monkeypatch):
    svc = _setup(tmp_path, monkeypatch)
    t1 = svc.create("t1")
    svc.update(t1.id, workflow_step="implement_tasks", status="in_progress")
    t2 = svc.create("t2")
    svc.update(t2.id, workflow_step="implement_tasks", status="in_progress")
    t3 = svc.create("t3")
    svc.update(t3.id, workflow_step="implement_tasks", status="done")
    t4 = svc.create("t4")
    svc.update(t4.id, workflow_step="green_gate", status="in_progress")

    result = workflow_live.live_for_project("test")

    assert result["conductor"]["occupancy"]["implement_tasks"] == 2
    assert result["conductor"]["occupancy"]["green_gate"] == 1
    assert result["conductor"]["occupancy"]["story_gate"] == 0


def test_conductor_live_reports_the_freshest_beating_task(tmp_path, monkeypatch):
    svc = _setup(tmp_path, monkeypatch)
    task = svc.create("t1")
    svc.update(task.id, workflow_step="implement_tasks", status="in_progress")
    _beat(tmp_path, task.id, "implement_tasks", node="text-challenge",
          driver="prism-task-runner")

    result = workflow_live.live_for_project("test")

    live_node = result["conductor"]["live"]["implement_tasks"]
    assert live_node["task_id"] == task.id
    assert live_node["driver"] == "prism-task-runner"


def test_exactly_one_latest_many_and_one_task_listing_per_call(tmp_path, monkeypatch):
    svc = _setup(tmp_path, monkeypatch)
    task = svc.create("t1")
    svc.update(task.id, workflow_step="implement_tasks", status="in_progress")
    _beat(tmp_path, task.id, "implement_tasks", node="loop")

    heartbeat_calls = []
    real_latest_many = drive_heartbeat.latest_many

    def _counting_latest_many(scores_db, task_ids):
        heartbeat_calls.append(list(task_ids))
        return real_latest_many(scores_db, task_ids)

    monkeypatch.setattr(drive_heartbeat, "latest_many", _counting_latest_many)

    connect_calls = []
    real_connect = sqlite_db.connect

    def _counting_connect(path, **kwargs):
        connect_calls.append(path)
        return real_connect(path, **kwargs)

    monkeypatch.setattr(workflow_live, "sqlite_db",
                         types.SimpleNamespace(connect=_counting_connect))

    workflow_live.live_for_project("test")

    assert len(heartbeat_calls) == 1, heartbeat_calls
    assert len(connect_calls) == 1, connect_calls
