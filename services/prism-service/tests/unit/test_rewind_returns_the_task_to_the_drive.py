"""A rewind returns the task to the drive (task 6f18c224).

`ConductorService._auto_rewind` moves a task back to a step an AGENT must
work, and clears blocked_reason -- but it never wrote `status`. A task a
sweep had parked as blocked kept status="blocked" with an EMPTY reason, and
every sweep reads only in_progress rows (task_runner.py:717,
resume_actuator.py:120/140, ship_worker.py:817/937), so nothing drove it
again and no reason text said why.

AC-1 blocked -> in_progress | AC-2 a surviving reason keeps the block
AC-3 only a block is lifted | AC-4 the row enters the drive sweep list.
"""
from types import SimpleNamespace

from prism_service.services.conductor_service import ConductorService

REASON = "green_gate reject: the fix does not cover the diverged case"


class FakeTaskService:
    def __init__(self, task):
        self.task, self.rows = task, []

    def get(self, task_id):
        return self.task

    def list(self, status=None, **kw):
        """The one filter every drive sweep uses."""
        if status is not None and getattr(self.task, "status", "") != status:
            return []
        return [self.task]

    def update(self, task_id, **fields):
        for k, v in fields.items():
            setattr(self.task, k, v)
        return self.task

    def record_history(self, task_id, action, **kw):
        self.rows.append(SimpleNamespace(action=action, actor=kw.get("actor"),
                                         model=kw.get("model"),
                                         details=kw.get("details", ""),
                                         reason=kw.get("reason", "")))

    def history(self, task_id):
        return list(self.rows)


def _svc(status="blocked", blocked_reason="ship failed: push rejected"):
    task = SimpleNamespace(id="t1", workflow_step="green_gate",
                           gate_state="failed", gate_reason="",
                           status=status, blocked_reason=blocked_reason)
    svc = ConductorService.__new__(ConductorService)
    svc._task_svc = FakeTaskService(task)
    svc._project_name = "default"
    return svc, task, svc._task_svc


def test_a_rejected_gate_returns_a_blocked_task_to_the_drive():
    """AC-1: the rewind owns the status, because it lands on an agent step."""
    svc, task, fake = _svc()
    svc._auto_rewind("t1", "implement_tasks", REASON, "manual reject")
    assert task.workflow_step == "implement_tasks"
    assert task.gate_state == "none"
    assert task.status == "in_progress"
    assert task.blocked_reason == ""
    assert [r.action for r in fake.rows] == ["auto_rewind"]


def test_a_surviving_reason_keeps_the_block():
    """AC-2: a block a person set on purpose must not disappear."""
    svc, task, _ = _svc()
    svc._auto_rewind("t1", "implement_tasks", REASON, "manual reject",
                     keep_block=True)
    assert task.status == "blocked"
    assert task.blocked_reason == "ship failed: push rejected"


def test_the_rewind_only_lifts_a_block():
    """AC-3: any other status is left exactly as it was."""
    for status in ("in_progress", "pending", "done"):
        svc, task, _ = _svc(status=status, blocked_reason="")
        svc._auto_rewind("t1", "implement_tasks", REASON, "manual reject")
        assert task.status == status


def test_the_rewound_task_is_eligible_for_the_drive():
    """AC-4: the row now answers the sweep's own status filter."""
    svc, task, fake = _svc()
    assert fake.list(status="in_progress") == []
    svc._auto_rewind("t1", "implement_tasks", REASON, "manual reject")
    assert [t.id for t in fake.list(status="in_progress")] == ["t1"]
