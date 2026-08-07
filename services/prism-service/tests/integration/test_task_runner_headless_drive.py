"""Task runner drives a task WITHOUT a human terminal (epic 0784729f,
AC-4). Pins the opt-in/off-by-default contract, one-task-one-step-per-
tick, gate-skip, budget passthrough, and that a run leaves proof in the
claude-runs log — closing the exact failure class task f4dd3687
recorded (a green slice whose production code nothing constructs). See
also prism_service/main.py's lifespan, which must wire start_task_runner
in for real.
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
    """A task under a fresh throwaway project. Tracks task ids so their
    REAL per-task git workspace is torn down after the test."""
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(**kwargs):
        project = "tr-" + uuid.uuid4().hex[:8]
        ctx = get_project(project)
        title = kwargs.pop("title", "runner task")
        task = ctx.task_svc.create(title=title, **kwargs)
        created.append(task.id)
        return ctx, task, project

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


class _FakeResult:
    def __init__(self, text: str, exit_code: int = 0, run_id: str = "run-x"):
        self._text = text
        self.exit_code = exit_code
        self.run_id = run_id

    def final_text(self) -> str:
        return self._text


def _fake_invoke(calls, text="## Premises\n- ok - UNVERIFIED\n",
                 exit_code=0):
    def _invoke(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        return _FakeResult(text, exit_code=exit_code)
    return _invoke


# ---------------------------------------------------------------------------
# Opt-in / off-by-default (mirrors gate_adjudicator's contract exactly)
# ---------------------------------------------------------------------------

def test_disabled_by_default_no_thread_no_calls(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.delenv("PRISM_TASK_RUNNER_INTERVAL", raising=False)
    assert tr.is_enabled() is False
    assert tr.start_task_runner() is None
    import threading
    assert not any(t.name == "prism-task-runner"
                   for t in threading.enumerate())


def test_env_var_opts_in(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.setenv("PRISM_TASK_RUNNER_INTERVAL", "5")
    assert tr.is_enabled() is True
    assert tr._interval_s() == 5


# ---------------------------------------------------------------------------
# One tick advances exactly one step of one eligible task
# ---------------------------------------------------------------------------

def test_one_tick_advances_exactly_one_step(make_task, monkeypatch):
    from prism_service.api import conductor_flow as cf
    from prism_service.services import task_runner as tr
    from prism_service.inference import claude_cli

    ctx, task, project = make_task()
    start = cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                          project=project)
    assert start["ok"] is True, start
    assert start["job"]["step"] == "review_previous_notes"
    ctx.task_svc.update(task.id, status="in_progress")

    calls = []
    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke(calls))

    res = tr.run_one_step(project, task.id)
    assert res["ok"] is True, res
    assert len(calls) == 1, "one tick must invoke claude exactly once"

    after = ctx.task_svc.get(task.id)
    assert after.workflow_step == "draft_story", after.workflow_step
    assert "UNVERIFIED" in (after.premise_notes or "")


# ---------------------------------------------------------------------------
# A task at a pending gate is SKIPPED and never gate-decided
# ---------------------------------------------------------------------------

def test_task_at_pending_gate_is_skipped(make_task, monkeypatch):
    from prism_service.services import task_runner as tr
    from prism_service.inference import claude_cli

    ctx, task, project = make_task()
    ctx.task_svc.update(task.id, workflow_step="story_gate",
                        gate_state="pending", status="in_progress")

    assert tr.eligible_task(project) is None

    calls = []
    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke(calls))
    res = tr.run_one_step(project, task.id)

    assert res["ok"] is False
    assert calls == [], "a gate step must never reach claude_cli"
    still = ctx.task_svc.get(task.id)
    assert still.gate_state == "pending"
    assert still.workflow_step == "story_gate"


# ---------------------------------------------------------------------------
# Budget / turn ceiling passthrough
# ---------------------------------------------------------------------------

def test_budget_and_turns_pass_through(make_task, monkeypatch):
    from prism_service.api import conductor_flow as cf
    from prism_service.services import task_runner as tr
    from prism_service.inference import claude_cli

    ctx, task, project = make_task()
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                 project=project)
    ctx.task_svc.update(task.id, status="in_progress")

    monkeypatch.setenv("PRISM_TASK_RUNNER_MAX_TURNS", "7")
    monkeypatch.setenv("PRISM_TASK_RUNNER_MAX_BUDGET_USD", "0.5")
    calls = []
    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke(calls))

    tr.run_one_step(project, task.id)
    assert calls[0]["max_turns"] == 7
    assert calls[0]["max_budget_usd"] == 0.5


# ---------------------------------------------------------------------------
# A real run leaves proof in /api/claude-runs — nobody watching still
# leaves a receipt. Fakes subprocess.run only (never claude_cli.invoke
# itself), so the REAL record-a-run pipeline is exercised end to end.
# ---------------------------------------------------------------------------

def test_run_appears_in_claude_run_log(make_task, monkeypatch):
    from prism_service.api import conductor_flow as cf
    from prism_service.services import task_runner as tr
    from prism_service.inference import claude_cli
    from prism_service.services import claude_run_log

    ctx, task, project = make_task()
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                 project=project)
    ctx.task_svc.update(task.id, status="in_progress")

    stream_line = (
        '{"type":"result","usage":{"input_tokens":10,"output_tokens":5},'
        '"total_cost_usd":0.01}\n'
        '{"message":{"content":[{"type":"text","text":'
        '"## Premises\\n- ok - UNVERIFIED\\n"}]}}\n')

    def _fake_run(cmd, cwd=None, env=None, stdout=None, stderr=None):
        stdout.write(stream_line)

        class _R:
            returncode = 0
            stderr = b""
        return _R()

    monkeypatch.setattr(claude_cli.subprocess, "run", _fake_run)

    res = tr.run_one_step(project, task.id)
    assert res["ok"] is True, res

    rows = claude_run_log.list_recent(project=project)
    assert rows, "the run must be recorded even though nobody was watching"
    assert rows[0]["purpose"].startswith("task-runner@")


# ---------------------------------------------------------------------------
# It is WIRED: production code actually constructs the runner (guards the
# epic's own recorded failure — task f4dd3687: a green slice nothing
# produces).
# ---------------------------------------------------------------------------

def test_wired_into_the_lifespan():
    import inspect
    import prism_service.main as main

    src = inspect.getsource(main.lifespan)
    assert "start_task_runner" in src, (
        "start_task_runner() must be called from the real lifespan, or "
        "PRISM_TASK_RUNNER_INTERVAL has nothing constructing it")
