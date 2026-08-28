"""A reported step FAILURE must refresh a live-open browser tab (task
1728c54b).

CONFIRMED BUG: api/conductor_flow.py's `flow_report`, the `elif failed:`
branch, records the failure via `svc._task_svc.record_history(...)` and
returns WITHOUT ever calling `task_svc.update()`. `record_history` is a
raw history-table insert — it never computes a fresh `activity` block and
never publishes the `task.changed` bus event that TaskDetailPage.tsx's
`/sse/tasks` subscription relies on. Result, confirmed live: a step fails,
`GET /api/tasks/{id}` immediately shows the correct `activity.state:
"stalled"`, but an already-open tab keeps showing the pre-failure state
(e.g. "driving") forever, until a manual reload.

FIX: TaskService.update()'s activity-compute + bus-publish + task_runner
wake block is extracted into `_publish_task_changed(task, fields)`; a new
`publish_activity_changed(task_id)` loads the task, computes a fresh
`activity` block the same way, and publishes with `fields={}` (no column
actually changed). `flow_report`'s failure branch calls it right after
`record_history`, swallowing any exception exactly like that call.

Pinned here:
  AC-1 -- a failure report through flow_report's real `elif failed:`
          branch publishes a `task.changed` event on the real bus, for a
          task with a real ConductorService attached, carrying a fresh
          (non-None) `activity` block -- not just a history row.
  AC-2 -- publish_activity_changed() never raises when no
          ConductorService is attached (bare TaskService).
  AC-3 -- publish_activity_changed() never raises when activity_for
          itself raises.
"""

from __future__ import annotations

import asyncio
import uuid

_SEAT = "test-worker-session"


def _project() -> str:
    return "flowreport-" + uuid.uuid4().hex[:8]


async def _drain_after(coro_body):
    """Subscribe to the REAL prism_service.events.bus (no injected
    double), run `coro_body(q)`, and return whatever landed on the queue.
    Mirrors test_task_page_payload_scope.py's own helper of the same
    name."""
    from prism_service.events import bus

    q = bus.subscribe()
    try:
        await coro_body(q)
        await asyncio.sleep(0)  # let call_soon_threadsafe callbacks land
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        return out
    finally:
        bus.unsubscribe(q)


def test_flow_report_failure_publishes_task_changed_with_fresh_activity():
    from prism_service.api import conductor_flow as flow
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    project = _project()
    ctx = get_project(project)
    # Force conductor_svc to materialize NOW (mirrors what `_svc(project)`
    # inside flow_report does) so task_svc.attach_conductor_service has
    # already run before we create the task.
    _ = ctx.conductor_svc

    task = ctx.task_svc.create(title="a step that will fail")
    ctx.task_svc.update(
        task.id, status="in_progress",
        workflow_step="implement_tasks", gate_state="none")

    async def body(q):
        while not q.empty():
            q.get_nowait()  # drop create/update noise from setup above
        result = flow.flow_report(
            flow.Ident(
                task_id=task.id,
                session_id=_SEAT,
                outcome="failed",
                expected_step="implement_tasks",
            ),
            project=project,
        )
        body.result = result

    try:
        events = asyncio.run(_drain_after(body))
        result = body.result

        assert result.get("ok") is False and result.get("advanced") is False, (
            f"a reported failure must not advance the step; got {result!r}")

        changed = [e for e in events if e.get("type") == "task.changed"
                   and e.get("task_id") == task.id]
        assert changed, (
            "a step-failure report must publish a task.changed event so an "
            f"already-open tab refreshes over /sse/tasks; got events={events!r}")
        evt = changed[-1]
        assert evt.get("activity") is not None, (
            "the published event must carry a FRESHLY computed `activity` "
            f"block, not just a bare history row; got {evt!r}")
        assert isinstance(evt.get("activity"), dict) and "state" in evt["activity"], (
            f"activity must be a real activity_for() block with a `state` "
            f"key; got {evt!r}")
    finally:
        tw.remove_workspace(task.id)


def test_publish_activity_changed_never_raises_without_conductor(tmp_path):
    from prism_service.services.task_service import TaskService

    svc = TaskService(str(tmp_path / "tasks.db"), project="prism")
    task = svc.create(title="bare task service, no conductor attached")

    # Must not raise even though no ConductorService was ever attached.
    svc.publish_activity_changed(task.id)


def test_publish_activity_changed_never_raises_when_activity_for_throws(tmp_path):
    from prism_service.services.task_service import TaskService

    class _BoomConductor:
        def phase_progress(self, task_id):
            return {}

        def activity_for(self, task, phase_progress):
            raise RuntimeError("activity computation blew up")

    svc = TaskService(str(tmp_path / "tasks.db"), project="prism")
    svc.attach_conductor_service(_BoomConductor())
    task = svc.create(title="a task whose conductor is broken")

    # Must not raise even though activity_for() raises internally.
    svc.publish_activity_changed(task.id)


def test_publish_activity_changed_never_raises_for_an_unknown_task(tmp_path):
    from prism_service.services.task_service import TaskService

    svc = TaskService(str(tmp_path / "tasks.db"), project="prism")

    # Must not raise, and must simply no-op, for a task_id that doesn't exist.
    svc.publish_activity_changed("no-such-task-id")
