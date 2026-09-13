"""Tick-cost pass (owner brief, 2026-09-13): sweep_once must not pay a
project-wide cost proportional to EVERY task in the project, and a second
sweep over a set of pending gates that has not moved must do zero further
adjudication work.

Measured live: gate_adjudicator.sweep_once took 136.8s then 94s+ per pass
with ZERO in_progress tasks and 26 tasks sitting at pending gates, none of
them changed between passes. Two separate cost sources are pinned here:

  1. sweep_once used to call TaskService.list() -- a project-wide
     SELECT * plus a full row_to_task conversion (JSON-decoding
     dependencies/tags, reading every text column) -- once per project,
     per pass, regardless of how many of those tasks were gate-relevant.
     It must instead call the new lean TaskService.gate_sweep_rows(),
     which narrows both the predicate and the columns at the SQL layer.
  2. the existing per-task backoff (_BACKOFF, task 72ccaf94-era) must
     still hold across the lean snapshot: a task whose lean row is
     unchanged must not be re-adjudicated on the very next pass.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import gate_adjudicator as ga  # noqa: E402


class _FakeTaskSvc:
    """Records every call to `list()` (must stay zero) and `gate_sweep_rows()`
    (must be the ONLY source of the sweep's task snapshot)."""

    def __init__(self, rows):
        self._rows = rows
        self.list_calls = 0
        self.gate_sweep_rows_calls = 0

    def list(self, *a, **kw):
        self.list_calls += 1
        return self._rows

    def gate_sweep_rows(self):
        self.gate_sweep_rows_calls += 1
        return self._rows

    def get(self, tid):
        for r in self._rows:
            if r["id"] == tid:
                return dict(r)
        return None

    def update(self, tid, **kw):
        for r in self._rows:
            if r["id"] == tid:
                r.update(kw)


class _FakeConductorSvc:
    def __init__(self):
        self.adjudicate_calls = 0

    def adjudicate_green_gate(self, tid):
        self.adjudicate_calls += 1
        return None  # a stable refusal -- never approves


class _FakeCtx:
    def __init__(self, task_svc, conductor_svc):
        self.task_svc = task_svc
        self.conductor_svc = conductor_svc


def _pending_green_gate_rows(n):
    return [
        {"id": f"t{i}", "workflow_step": "green_gate", "gate_state": "pending",
         "updated_at": "2026-09-13T00:00:00+00:00"}
        for i in range(n)
    ]


def _install_fake_project(monkeypatch, ctx):
    from prism_service import project_context

    monkeypatch.setattr(project_context, "get_all_projects", lambda: ["proj"])
    monkeypatch.setattr(project_context, "get_project", lambda pid: ctx)
    monkeypatch.setattr(ga, "_pending_decline_reason", lambda *a, **k: "")


def setup_function(_):
    ga._BACKOFF.clear()


def test_gate_sweep_rows_is_lean_and_excludes_non_gate_tasks(tmp_path):
    """Real TaskService, real sqlite: the predicate excludes ordinary
    in-flight tasks (an agent step, no gate) and returns plain dicts with
    only the four columns the sweep reads -- not a full Task dataclass."""
    from prism_service.services.task_service import TaskService

    svc = TaskService(str(tmp_path / "tasks.db"))
    gated = svc.create(title="pending green gate")
    svc.update(gated.id, workflow_step="green_gate", gate_state="pending")
    done = svc.create(title="terminal")
    svc.update(done.id, workflow_step="done", gate_state="none")
    mid_flight = svc.create(title="still being written")
    svc.update(mid_flight.id, workflow_step="write_failing_tests",
               gate_state="none")

    rows = svc.gate_sweep_rows()
    ids = {r["id"] for r in rows}

    assert gated.id in ids and done.id in ids
    assert mid_flight.id not in ids, (
        "an agent-step task with no gate must never cost a sweep pass "
        "anything -- it is not in the lean snapshot at all")
    row = next(r for r in rows if r["id"] == gated.id)
    assert set(row.keys()) == {"id", "workflow_step", "gate_state", "updated_at"}


def test_sweep_once_uses_the_lean_snapshot_never_the_full_list(monkeypatch):
    task_svc = _FakeTaskSvc(_pending_green_gate_rows(5))
    ctx = _FakeCtx(task_svc, _FakeConductorSvc())
    _install_fake_project(monkeypatch, ctx)

    ga.sweep_once()

    assert task_svc.gate_sweep_rows_calls == 1
    assert task_svc.list_calls == 0, (
        "sweep_once must read the lean gate snapshot, never the "
        "project-wide list()")


def test_a_second_sweep_over_unchanged_pending_gates_adjudicates_nothing(
        monkeypatch):
    """The actual cost claim: pass 1 pays for real adjudication on every
    eligible task; pass 2, with nothing changed, must cost zero
    adjudicate_green_gate calls and complete in well under a second."""
    rows = _pending_green_gate_rows(26)
    task_svc = _FakeTaskSvc(rows)
    conductor_svc = _FakeConductorSvc()
    ctx = _FakeCtx(task_svc, conductor_svc)
    _install_fake_project(monkeypatch, ctx)

    ga.sweep_once()
    first_pass_calls = conductor_svc.adjudicate_calls
    assert first_pass_calls == 26, "every pending gate must be tried once"

    started = time.monotonic()
    ga.sweep_once()
    elapsed = time.monotonic() - started

    assert conductor_svc.adjudicate_calls == first_pass_calls, (
        "an unchanged pending gate must not be re-adjudicated on the very "
        "next pass -- this is the backoff the lean snapshot must not break")
    assert elapsed < 0.5, f"an all-backed-off pass took {elapsed:.3f}s"
    assert ga._last_eligible_count == 26
    assert ga._last_changed_count == 0, (
        "the pass detail's own 'changed' count must reflect the backoff "
        "holding, not just the eligible count")
