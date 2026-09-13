"""Idempotent update pass (owner brief, 2026-09-13): a worker sweep that
re-writes a task with the SAME values it already holds (a gate_reason
touch, a re-set status, a redundant blocked_reason) must cost the daemon
nothing — no SQL write, no wakeups.signal, no task_history row — or every
such no-op re-write cascades into another worker waking on task_changed
and doing its own pass, which is exactly the idle-system churn this
family of fixes targets.

TaskService.update() already short-circuits on `if not changes: return
task` before ever reaching the UPDATE statement or _publish_task_changed
(and therefore wakeups.signal). This pins that contract at the seam the
standing workers actually call, so a future edit cannot silently move a
write or a signal ahead of the no-op guard.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import wakeups  # noqa: E402
from prism_service.services.task_service import TaskService  # noqa: E402


def _svc(tmp_path) -> TaskService:
    return TaskService(str(tmp_path / "tasks.db"), project="proj")


def test_update_with_identical_values_makes_zero_sql_writes_and_zero_signals(
        tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    task = svc.create(title="pin idempotent update", description="d")
    svc.update(task.id, workflow_step="green_gate", gate_state="pending",
               gate_reason="not ready")

    before = svc.get(task.id)
    history_len_before = len(svc.history(task.id))

    signalled: list[tuple] = []
    monkeypatch.setattr(wakeups, "signal",
                         lambda *a, **k: signalled.append((a, k)))

    executes: list[str] = []
    svc._db.set_trace_callback(executes.append)
    try:
        # Re-send the exact same values a sweep would re-derive.
        result = svc.update(task.id, workflow_step="green_gate",
                             gate_state="pending", gate_reason="not ready")
    finally:
        svc._db.set_trace_callback(None)

    update_statements = [s for s in executes if s.strip().startswith("UPDATE tasks")]
    assert update_statements == [], (
        "a no-op update must never reach the UPDATE tasks statement — "
        f"got {update_statements!r}")
    assert signalled == [], (
        "a no-op update must never call wakeups.signal — "
        f"got {signalled!r}")
    assert len(svc.history(task.id)) == history_len_before, (
        "a no-op update must never write a new task_history row")

    after = svc.get(task.id)
    assert after.updated_at == before.updated_at, (
        "a no-op update must never touch updated_at")
    assert result is not None


def test_update_with_one_real_change_still_writes_and_signals(
        tmp_path, monkeypatch):
    """Sanity companion: the no-op guard above must not swallow a GENUINE
    change — a single differing field still reaches the write and the
    signal, so the idle-pass fix never masks real work."""
    svc = _svc(tmp_path)
    task = svc.create(title="pin idempotent update - real change")

    signalled: list[tuple] = []
    monkeypatch.setattr(wakeups, "signal",
                         lambda *a, **k: signalled.append((a, k)))

    svc.update(task.id, gate_state="pending")

    assert signalled != [], "a genuine field change must still signal"
    after = svc.get(task.id)
    assert after.gate_state == "pending"
