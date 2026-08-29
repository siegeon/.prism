"""A killed step says it was killed, and a slow step gets room to finish.

Live on 2026-08-29, epic 9f60a849 ("Drives finish without a human nudge")
stalled three times at verify_green_state. The recorded outcome was
`exit=-9, non-graceful failure` -- SIGKILL, the step hitting the 900 s
wall-clock bound while running the full suite. The host had 85 GB free and
no OOM kills, so this was the timeout, not memory.

Two customer-visible problems, both fixed here:

1. The task then reported "no red test id was named in the last proof".
   That is the fallback text for a stall with no test ids to split on, and
   it sends whoever reads it hunting for a test problem that does not
   exist. A step that was KILLED must say so.

2. verify_green_state runs the whole suite; 900 s is the same budget as a
   step that only writes a paragraph. The implement workflow already knows
   some steps are slow and gives them a multiple of the budget; the task
   runner did not.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


class _Task:
    def __init__(self):
        self.id = "9f60a849-4767-4129-88fd-b5322ce80bf3"
        self.completion_proof = "no test ids here"
        self.priority = 90
        self.tags = []
        self.status = "in_progress"


class _Svc:
    def __init__(self, history_rows=()):
        self.task = _Task()
        self.created = []
        self.updates = []
        self.recorded = []
        self._history = list(history_rows)

    def get(self, tid):
        return self.task

    def history(self, tid):
        return self._history

    def create(self, **kw):
        self.created.append(kw)
        return type("C", (), {"id": "child"})()

    def update(self, tid, **kw):
        self.updates.append(kw)

    def record_history(self, tid, **kw):
        self.recorded.append(kw)


class _Row:
    def __init__(self, action, details):
        self.action = action
        self.details = details


def test_a_slow_step_gets_a_bigger_budget_than_a_writing_step(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.delenv("PRISM_TASK_RUNNER_STEP_TIMEOUT_S", raising=False)
    base = tr._step_timeout_s("draft_story")
    slow = tr._step_timeout_s("verify_green_state")
    assert slow > base, (
        f"verify_green_state runs the whole suite and got the same {base}s "
        "budget as a step that writes a paragraph")
    assert slow >= base * 2


def test_the_budget_still_honours_the_environment(monkeypatch):
    """An operator who sets the variable still governs the base."""
    from prism_service.services import task_runner as tr

    monkeypatch.setenv("PRISM_TASK_RUNNER_STEP_TIMEOUT_S", "100")
    assert tr._step_timeout_s("draft_story") == 100.0
    assert tr._step_timeout_s("verify_green_state") >= 200.0


def test_a_stall_after_a_kill_says_it_was_killed(monkeypatch):
    """The honesty half. exit=-9 is SIGKILL: the step ran out of time, it
    did not fail to name a test."""
    from prism_service.services import task_runner as tr

    monkeypatch.setattr(tr, "_stall_work_is_shipped", lambda tid: False,
                        raising=False)
    svc = _Svc([_Row("flow_report_failure",
                     "step=verify_green_state; outcome={'ok': False, "
                     "'reason': 'exit=-9, non-graceful failure "
                     "(crash/auth/truncated mid-turn)'}")])
    tr._handle_stall(svc, svc.task.id, "verify_green_state")
    reason = " ".join(str(u.get("blocked_reason", "")) for u in svc.updates)
    assert "killed" in reason.lower(), (
        f"a SIGKILLed step must say so, not blame a missing test id: {reason}")
    assert "no red test id" not in reason, (
        "the misleading fallback text survived a kill")


def test_an_ordinary_stall_keeps_its_existing_reason(monkeypatch):
    """The guard: only a kill changes the message."""
    from prism_service.services import task_runner as tr

    monkeypatch.setattr(tr, "_stall_work_is_shipped", lambda tid: False,
                        raising=False)
    svc = _Svc([_Row("flow_report_failure",
                     "step=implement_tasks; outcome={'ok': False, "
                     "'reason': 'exit=1, non-graceful failure'}")])
    tr._handle_stall(svc, svc.task.id, "implement_tasks")
    reason = " ".join(str(u.get("blocked_reason", "")) for u in svc.updates)
    assert "no red test id was named" in reason, reason
    assert "killed" not in reason.lower()
