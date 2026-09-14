"""The runner's activity row names the drive, not a sweep (owner
2026-09-14: "it's a REACTIVE system, what sweeping is there really").

The loop has no clock: it blocks on a wake event, and the pass that
follows a wake lists eligible tasks and then BLOCKS inside run_one_step
for the whole inference call. The running row read "sweep_once" for all
of it, so a 480 s drive of bb3d1f6a's implement_tasks looked like a tick
that did nothing, while a65c66e5 queued behind it with no visible reason.

Pins:
- AC-1  sweep_once(info) flips the live pass entry's detail to
        "driving <task8> <step>" the moment it picks a task.
- AC-2  no pick leaves the entry's detail untouched.
- AC-3  _loop's pass_ label no longer says "sweep_once".
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from prism_service.services import task_runner


def _quiet_breakers(monkeypatch):
    monkeypatch.setattr(task_runner, "_spend_ceiling_crossed", lambda: False)
    monkeypatch.setattr(task_runner, "_system_overloaded", lambda: False)
    monkeypatch.setattr(task_runner, "_engine_unreachable", lambda: False)
    monkeypatch.setattr(task_runner, "_concurrency", lambda: 1)
    import prism_service.project_context as pc
    monkeypatch.setattr(pc, "get_all_projects", lambda: ["prism"])


# AC-1 ------------------------------------------------------------------
def test_the_running_row_names_the_task_and_step(monkeypatch):
    _quiet_breakers(monkeypatch)
    monkeypatch.setattr(task_runner, "eligible_tasks",
                        lambda pid, limit: ["a65c66e5-b8a7-44b4-a223-f1342cfaaa14"])
    monkeypatch.setattr(task_runner, "_step_label",
                        lambda pid, tid: "verify_plan")
    monkeypatch.setattr(task_runner, "run_one_step",
                        lambda pid, tid: {"ok": True, "task_id": tid})
    info = {"detail": "woke: checking eligible tasks"}
    task_runner.sweep_once(info)
    assert info["detail"] == "driving a65c66e5 verify_plan"


def test_a_step_label_failure_degrades_to_the_task_alone(monkeypatch):
    _quiet_breakers(monkeypatch)
    monkeypatch.setattr(task_runner, "eligible_tasks",
                        lambda pid, limit: ["bb3d1f6a-c3ff-488b-a754-010a7705907f"])
    monkeypatch.setattr(task_runner, "run_one_step",
                        lambda pid, tid: {"ok": True, "task_id": tid})
    import prism_service.project_context as pc

    def _boom(project):
        raise RuntimeError("no project")
    monkeypatch.setattr(pc, "get_project", _boom)
    info: dict = {}
    task_runner.sweep_once(info)
    assert info["detail"] == "driving bb3d1f6a"


# AC-2 ------------------------------------------------------------------
def test_no_pick_leaves_the_detail_alone(monkeypatch):
    _quiet_breakers(monkeypatch)
    monkeypatch.setattr(task_runner, "eligible_tasks", lambda pid, limit: [])
    info = {"detail": "woke: checking eligible tasks"}
    assert task_runner.sweep_once(info) is None
    assert info["detail"] == "woke: checking eligible tasks"


# AC-3 ------------------------------------------------------------------
def test_the_loop_label_does_not_say_sweep():
    src = inspect.getsource(task_runner._loop)
    assert '"sweep_once"' not in src
    assert "woke" in src
    assert "sweep_once(info)" in src
