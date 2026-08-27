"""Host-load circuit breaker for the server-side task runner (real
incident, 2026-08-26): 30+ PRISM tasks sat `in_progress` simultaneously,
each one `task_runner`'s drive worker is willing to spawn a real
`claude -p` subprocess for, competing with many other concurrent agent/
pytest processes already on the host -- one of that night's own
background fixes was independently killed by its own internal timeout
purely from host contention. `task_runner.py` already has a cost-based
circuit breaker (`_spend_ceiling_crossed`) checked at both `eligible_
task()` and `sweep_once()`; this pins the equivalent host-load guard
(`_system_overloaded`, `PRISM_TASK_RUNNER_MAX_LOAD_PER_CORE`), wired at
the same two call sites.

Unlike the spend ceiling (unset == unbounded/inactive), the load
ceiling carries a real numeric default and is ACTIVE the moment
task_runner itself is enabled -- it is a genuine safety rail, not a
second opt-in a environment could forget to set.
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
def make_task():
    """A task under a fresh throwaway project, with its real per-task git
    workspace torn down afterwards (mirrors test_runner_usage_persisted's
    fixture)."""
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(**kwargs):
        project = "load-breaker-" + uuid.uuid4().hex[:8]
        ctx = get_project(project)
        task = ctx.task_svc.create(title=kwargs.pop("title", "load task"),
                                   **kwargs)
        created.append(task.id)
        return ctx, task, project

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


# ---------------------------------------------------------------------------
# Pure threshold math: _max_load_per_core() / _system_overloaded()
# ---------------------------------------------------------------------------

def test_default_ceiling_is_eight_per_core_when_unset(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.delenv("PRISM_TASK_RUNNER_MAX_LOAD_PER_CORE", raising=False)
    assert tr._max_load_per_core() == 8.0


def test_ceiling_env_var_overrides_default(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.setenv("PRISM_TASK_RUNNER_MAX_LOAD_PER_CORE", "2.5")
    assert tr._max_load_per_core() == 2.5


def test_malformed_ceiling_env_var_falls_back_to_default(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.setenv("PRISM_TASK_RUNNER_MAX_LOAD_PER_CORE", "not-a-number")
    assert tr._max_load_per_core() == 8.0


def test_not_overloaded_when_load_at_the_ceiling(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.setenv("PRISM_TASK_RUNNER_MAX_LOAD_PER_CORE", "4.0")
    monkeypatch.setattr("os.getloadavg", lambda: (16.0, 16.0, 16.0))
    monkeypatch.setattr("os.cpu_count", lambda: 4)

    assert tr._system_overloaded() is False, (
        "load exactly AT the ceiling (16.0/4 == 4.0) must not trip it")


def test_not_overloaded_when_load_under_the_ceiling(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.setenv("PRISM_TASK_RUNNER_MAX_LOAD_PER_CORE", "8.0")
    monkeypatch.setattr("os.getloadavg", lambda: (3.74, 4.34, 3.60))
    monkeypatch.setattr("os.cpu_count", lambda: 24)

    assert tr._system_overloaded() is False, (
        "observed real quiet-box load (3.74/24 ~= 0.16 per core) must "
        "stay well clear of the default ceiling")


def test_overloaded_once_load_exceeds_the_ceiling(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.setenv("PRISM_TASK_RUNNER_MAX_LOAD_PER_CORE", "4.0")
    monkeypatch.setattr("os.getloadavg", lambda: (20.0, 20.0, 20.0))
    monkeypatch.setattr("os.cpu_count", lambda: 4)

    assert tr._system_overloaded() is True, (
        "20.0/4 == 5.0 per core exceeds the 4.0 ceiling")


def test_overloaded_is_logged(monkeypatch, capsys):
    from prism_service.services import task_runner as tr

    monkeypatch.setenv("PRISM_TASK_RUNNER_MAX_LOAD_PER_CORE", "4.0")
    monkeypatch.setattr("os.getloadavg", lambda: (20.0, 20.0, 20.0))
    monkeypatch.setattr("os.cpu_count", lambda: 4)

    assert tr._system_overloaded() is True
    captured = capsys.readouterr()
    assert "overloaded" in captured.err
    assert "PRISM_TASK_RUNNER_MAX_LOAD_PER_CORE" in captured.err


def test_getloadavg_raising_oserror_fails_safe(monkeypatch):
    """A platform/environment where os.getloadavg() is unavailable
    (Windows, some containers) must never crash the sweep loop -- treat
    it as not-overloaded, exactly like _spend_ceiling_crossed() fails
    safe when its own env var is unset."""
    from prism_service.services import task_runner as tr

    def _raise():
        raise OSError("getloadavg not supported")

    monkeypatch.setattr("os.getloadavg", _raise)

    assert tr._system_overloaded() is False


def test_getloadavg_raising_attributeerror_fails_safe(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.delattr("os.getloadavg", raising=False)

    assert tr._system_overloaded() is False


def test_getloadavg_raising_notimplementederror_fails_safe(monkeypatch):
    from prism_service.services import task_runner as tr

    def _raise():
        raise NotImplementedError

    monkeypatch.setattr("os.getloadavg", _raise)

    assert tr._system_overloaded() is False


def test_none_cpu_count_does_not_crash(monkeypatch):
    """os.cpu_count() can legitimately return None (undeterminable core
    count) -- must fall back to 1, never raise ZeroDivisionError/TypeError."""
    from prism_service.services import task_runner as tr

    monkeypatch.setenv("PRISM_TASK_RUNNER_MAX_LOAD_PER_CORE", "8.0")
    monkeypatch.setattr("os.getloadavg", lambda: (0.5, 0.5, 0.5))
    monkeypatch.setattr("os.cpu_count", lambda: None)

    assert tr._system_overloaded() is False


# ---------------------------------------------------------------------------
# Wired into eligible_task() and sweep_once() at the same call sites as
# the existing spend ceiling.
# ---------------------------------------------------------------------------

def test_eligible_task_refuses_when_overloaded(make_task, monkeypatch):
    from prism_service.services import task_runner as tr

    ctx, task, project = make_task()
    ctx.task_svc.update(task.id, status="in_progress")
    assert task.workflow_step == ""

    monkeypatch.setattr(tr, "_system_overloaded", lambda: True)

    assert tr.eligible_task(project) is None, (
        "eligible_task() must refuse to claim new work while the host is "
        "reported overloaded")


def test_eligible_task_proceeds_when_not_overloaded(make_task, monkeypatch):
    from prism_service.services import task_runner as tr

    ctx, task, project = make_task()
    ctx.task_svc.update(task.id, status="in_progress")
    assert task.workflow_step == ""

    monkeypatch.setattr(tr, "_system_overloaded", lambda: False)

    assert tr.eligible_task(project) == task.id


def test_sweep_once_skips_a_tick_when_overloaded(make_task, monkeypatch):
    from prism_service.services import task_runner as tr
    from prism_service.api import conductor_flow as cf
    from prism_service import project_context

    ctx, task, project = make_task()
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                 project=project)
    ctx.task_svc.update(task.id, status="in_progress")

    monkeypatch.setattr(project_context, "get_all_projects", lambda: [project])
    monkeypatch.setattr(tr, "_rr_index", 0)
    monkeypatch.setattr(tr, "_system_overloaded", lambda: True)

    run_calls = []
    monkeypatch.setattr(tr, "run_one_step",
                        lambda pid, tid: run_calls.append((pid, tid)))

    result = tr.sweep_once()

    assert result is None, result
    assert run_calls == [], (
        "sweep_once() must not call run_one_step at all while the host "
        "is reported overloaded")


def test_sweep_once_proceeds_when_not_overloaded(make_task, monkeypatch):
    from prism_service.services import task_runner as tr
    from prism_service.api import conductor_flow as cf
    from prism_service.inference import claude_cli
    from prism_service import project_context

    ctx, task, project = make_task()
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                 project=project)
    ctx.task_svc.update(task.id, status="in_progress")

    monkeypatch.setattr(project_context, "get_all_projects", lambda: [project])
    monkeypatch.setattr(tr, "_rr_index", 0)
    monkeypatch.setattr(tr, "_system_overloaded", lambda: False)

    class _FakeResult:
        def __init__(self):
            self.exit_code = 0
            self.run_id = "run-load-ok"
            self.usage = None

        def final_text(self):
            return "## Premises\n- ok - UNVERIFIED\n"

    monkeypatch.setattr(claude_cli, "invoke", lambda prompt, **kw: _FakeResult())

    result = tr.sweep_once()

    assert result is not None and result["ok"] is True, result
