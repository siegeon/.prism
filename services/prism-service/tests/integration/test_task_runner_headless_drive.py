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
# A fresh task (status=in_progress, workflow_step=="", never separately
# driven into the flow by a human/session) is eligible AND the runner
# bootstraps it itself -- epic 3baadd19's own oracle text ("set it
# in_progress and TOUCH NOTHING ELSE... PRISM claims it by itself") is not
# true unless a task that has never entered the flow is eligible: nothing
# else calls flow_start for it.
# ---------------------------------------------------------------------------

def test_fresh_in_progress_task_with_blank_workflow_step_is_eligible(
        make_task):
    from prism_service.services import task_runner as tr

    ctx, task, project = make_task()
    ctx.task_svc.update(task.id, status="in_progress")
    assert task.workflow_step == ""

    assert tr.eligible_task(project) == task.id


def test_runner_bootstraps_a_fresh_task_into_the_flow_end_to_end(
        make_task, monkeypatch):
    from prism_service.services import task_runner as tr
    from prism_service.inference import claude_cli

    ctx, task, project = make_task()
    ctx.task_svc.update(task.id, status="in_progress")
    assert task.workflow_step == ""

    calls = []
    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke(calls))

    res = tr.run_one_step(project, task.id)
    assert res["ok"] is True, res
    assert len(calls) == 1, "the runner's own flow_start must bootstrap " \
        "the flow and then drive its first step in the same tick"

    after = ctx.task_svc.get(task.id)
    assert after.workflow_step == "draft_story", after.workflow_step


# ---------------------------------------------------------------------------
# Round-robin project fairness: a project that ALWAYS has an eligible task
# (a continuous backlog) must not permanently starve every alphabetically-
# later project of the single per-tick work slot. Observed live (epic
# 3baadd19 AC-2, task 12403c60): "csregs-datamanagement" sorts before
# "prism" and held every tick for the runner's entire uptime.
# ---------------------------------------------------------------------------

def test_sweep_once_rotates_starting_project_so_none_can_starve(monkeypatch):
    from prism_service.services import task_runner as tr
    from prism_service import project_context

    monkeypatch.setattr(tr, "_rr_index", 0)
    monkeypatch.setattr(project_context, "get_all_projects",
                        lambda: ["p-alpha", "p-beta", "p-gamma"])
    # Every project is ALWAYS eligible -- the worst case for starvation.
    monkeypatch.setattr(tr, "eligible_task", lambda pid: f"{pid}-task")

    driven = []
    monkeypatch.setattr(tr, "run_one_step",
                        lambda pid, task_id: driven.append(pid) or
                        {"ok": True, "task_id": task_id})

    for _ in range(4):
        tr.sweep_once()

    assert driven == ["p-alpha", "p-beta", "p-gamma", "p-alpha"], driven


def test_a_starved_project_gets_its_turn_once_the_busy_one_clears(
        monkeypatch):
    from prism_service.services import task_runner as tr
    from prism_service import project_context

    monkeypatch.setattr(tr, "_rr_index", 0)
    monkeypatch.setattr(project_context, "get_all_projects",
                        lambda: ["p-busy", "p-starved"])
    # p-busy is eligible forever; p-starved is eligible from the start too
    # -- with NO rotation, p-busy would win on every single tick.
    monkeypatch.setattr(
        tr, "eligible_task",
        lambda pid: f"{pid}-task" if pid in ("p-busy", "p-starved") else None)

    driven = []
    monkeypatch.setattr(tr, "run_one_step",
                        lambda pid, task_id: driven.append(pid) or
                        {"ok": True, "task_id": task_id})

    tr.sweep_once()
    tr.sweep_once()

    assert "p-starved" in driven, driven


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
# Step timeout passthrough (epic 3baadd19 AC-1 wiring gap): claude_cli.invoke
# has supported timeout_s since af8ec904, but nothing called it with one --
# a wedged `claude -p` child could still hang the drive worker forever.
# ---------------------------------------------------------------------------

def test_step_timeout_passes_through_with_env_override(make_task, monkeypatch):
    from prism_service.api import conductor_flow as cf
    from prism_service.services import task_runner as tr
    from prism_service.inference import claude_cli

    ctx, task, project = make_task()
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                 project=project)
    ctx.task_svc.update(task.id, status="in_progress")

    monkeypatch.setenv("PRISM_TASK_RUNNER_STEP_TIMEOUT_S", "45")
    calls = []
    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke(calls))

    tr.run_one_step(project, task.id)
    assert calls[0]["timeout_s"] == 45.0


def test_step_timeout_defaults_when_env_unset(make_task, monkeypatch):
    from prism_service.api import conductor_flow as cf
    from prism_service.services import task_runner as tr
    from prism_service.inference import claude_cli

    ctx, task, project = make_task()
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                 project=project)
    ctx.task_svc.update(task.id, status="in_progress")

    monkeypatch.delenv("PRISM_TASK_RUNNER_STEP_TIMEOUT_S", raising=False)
    calls = []
    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke(calls))

    tr.run_one_step(project, task.id)
    assert calls[0]["timeout_s"] == 900.0


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

    def _fake_run(cmd, cwd=None, env=None, stdout=None, stderr=None,
                  timeout=None):
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


# ---------------------------------------------------------------------------
# Event-driven wake: a task becoming eligible must not sit out the full
# PRISM_TASK_RUNNER_INTERVAL when nothing else is fighting it for the slot.
# Observed live: a single, uncontested task waited ~16 real minutes for its
# retry because _loop only ever woke on a fixed clock. wake() lets the SAME
# task.changed write that already pushes to /sse/tasks also cut the wait
# short; the interval survives only as a fallback for changes this module
# never learns about.
# ---------------------------------------------------------------------------

def test_wake_cuts_the_wait_short_instead_of_sitting_out_the_interval():
    import threading
    import time as time_mod
    from prism_service.services import task_runner as tr

    sweeps = []
    swept = threading.Event()

    def _fake_sweep_once():
        sweeps.append(time_mod.monotonic())
        swept.set()
        return None

    tr.sweep_once = _fake_sweep_once
    # _wake_event is a module-level singleton shared with every real
    # task_svc.update() call in this process -- another test earlier in
    # this session may have already set it. Start from a known-clear state
    # so this test's own wake() is what's actually being observed.
    tr._wake_event.clear()
    try:
        # A deliberately huge interval -- if wake() didn't work, the second
        # sweep would never arrive within this test's timeout.
        t = threading.Thread(target=tr._loop, args=(999,), daemon=True)
        t.start()
        assert swept.wait(timeout=2), "first sweep (loop entry) never ran"
        swept.clear()

        tr.wake()

        assert swept.wait(timeout=2), (
            "wake() did not cut the 999s wait short -- the loop is still "
            "pure fixed-interval polling")
        assert len(sweeps) == 2
        assert sweeps[1] - sweeps[0] < 2, (
            "second sweep took nearly as long as the interval, not a wake")
    finally:
        # The spawned daemon thread above has no stop signal and outlives
        # this test for the rest of the pytest PROCESS (observed live on
        # CI: its real sweep_once() ticks interleaved into unrelated
        # tests' captured stderr, called by every later test's wake() via
        # the shared module-level _wake_event, causing intermittent
        # cross-test failures like a doubled flow_report call). Restoring
        # sweep_once to the REAL implementation here hands that leaked
        # thread live production behavior for the rest of the session --
        # leave it pointed at a permanent no-op instead so the immortal
        # thread is inert. The real implementation is deliberately never
        # restored here.
        tr.sweep_once = lambda: None


# ---------------------------------------------------------------------------
# Per-run session identity (task 12403c60, epic 3baadd19 AC-2): a runner-
# driven claude -p child must run under its own fresh session id, distinct
# from the constant SEAT_ID that gate reports keep using for distinct-actor
# identity -- otherwise every worker-driven step across every task collapses
# onto one indistinguishable identity on /live.
# ---------------------------------------------------------------------------

def test_run_one_step_passes_a_fresh_uuid_session_id_distinct_from_seat_id(
        make_task, monkeypatch):
    from prism_service.api import conductor_flow as cf
    from prism_service.services import task_runner as tr
    from prism_service.inference import claude_cli

    ctx, task, project = make_task()
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                 project=project)
    ctx.task_svc.update(task.id, status="in_progress")

    calls = []
    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke(calls))

    res = tr.run_one_step(project, task.id)
    assert res["ok"] is True, res

    session_id = calls[0].get("session_id")
    assert session_id, "claude_cli.invoke must receive a non-empty session_id"
    uuid.UUID(session_id)  # raises ValueError if not a real UUID
    assert session_id != tr.SEAT_ID, (
        "the per-run session id must not collapse onto the constant "
        "gate-report seat identity")


def test_run_one_step_keeps_seat_id_as_the_gate_report_identity(
        make_task, monkeypatch):
    """AC-4: the new per-run session id must be confined to the
    claude_cli.invoke call site -- flow.Ident/flow_report (the gate-report
    distinct-actor identity) must still carry SEAT_ID, unchanged."""
    from prism_service.api import conductor_flow as cf
    from prism_service.services import task_runner as tr
    from prism_service.inference import claude_cli

    ctx, task, project = make_task()
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                 project=project)
    ctx.task_svc.update(task.id, status="in_progress")

    calls = []
    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke(calls))

    reported = []
    orig_flow_report = cf.flow_report

    def _spy_flow_report(body, project="default"):
        reported.append(body.session_id)
        return orig_flow_report(body, project=project)

    monkeypatch.setattr(cf, "flow_report", _spy_flow_report)

    res = tr.run_one_step(project, task.id)
    assert res["ok"] is True, res

    assert reported == [tr.SEAT_ID], (
        "flow_report's session_id must stay SEAT_ID even though "
        "claude_cli.invoke now receives a distinct per-run session id")


def test_task_service_update_wakes_the_runner(make_task, monkeypatch):
    from prism_service.services import task_runner as tr

    woken = []
    monkeypatch.setattr(tr, "wake", lambda: woken.append(True))

    ctx, task, project = make_task()
    ctx.task_svc.update(task.id, status="in_progress")

    assert woken, (
        "task_service.update() must call task_runner.wake() so a task "
        "becoming eligible is swept immediately, not on the next fixed-"
        "interval tick")


# ---------------------------------------------------------------------------
# Drive heartbeat (epic 3baadd19 AC-3): a pre-call beat must land in
# drive_heartbeats BEFORE claude_cli.invoke is called, so a long-running
# child still reads as driving on /live instead of stalled/idle.
# ---------------------------------------------------------------------------

def _scores_db_for(project):
    from prism_service.project_context import get_project

    return str(get_project(project)._data_dir / "scores.db")


def test_heartbeat_recorded_before_claude_invoke_returns(make_task, monkeypatch):
    from prism_service.api import conductor_flow as cf
    from prism_service.services import task_runner as tr
    from prism_service.services import drive_heartbeat
    from prism_service.inference import claude_cli

    ctx, task, project = make_task()
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                 project=project)
    ctx.task_svc.update(task.id, status="in_progress")

    scores_db = _scores_db_for(project)
    seen_during_call = {}

    def _invoke(prompt, **kwargs):
        # The heartbeat must already be on file BEFORE this (potentially
        # long-running) call returns -- a post-call beat only proves the
        # step FINISHED, not that it was working during the call.
        seen_during_call["row"] = drive_heartbeat.latest(scores_db, task.id)
        return _FakeResult("## Premises\n- ok - UNVERIFIED\n")

    monkeypatch.setattr(claude_cli, "invoke", _invoke)

    res = tr.run_one_step(project, task.id)
    assert res["ok"] is True, res

    row = seen_during_call.get("row")
    assert row is not None, (
        "no drive_heartbeats row existed while claude_cli.invoke was "
        "running -- the heartbeat must be posted BEFORE the call, not "
        "only after it returns")
    assert row["last_tool"] == "claude_cli.invoke", row


def test_heartbeat_recorded_at_advances_across_two_ticks(make_task, monkeypatch):
    import time

    from prism_service.api import conductor_flow as cf
    from prism_service.services import task_runner as tr
    from prism_service.services import drive_heartbeat
    from prism_service.inference import claude_cli

    ctx, task, project = make_task()
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                 project=project)
    ctx.task_svc.update(task.id, status="in_progress")

    scores_db = _scores_db_for(project)
    calls = []
    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke(calls))

    tr.run_one_step(project, task.id)
    first = drive_heartbeat.latest(scores_db, task.id)
    assert first is not None, "first tick recorded no heartbeat at all"

    time.sleep(0.01)
    tr.run_one_step(project, task.id)
    second = drive_heartbeat.latest(scores_db, task.id)
    assert second is not None, "second tick recorded no heartbeat at all"
    assert second["recorded_at"] > first["recorded_at"], (
        "a second driven tick must advance recorded_at -- a single frozen "
        "beat does not satisfy a driver that is genuinely still working")
