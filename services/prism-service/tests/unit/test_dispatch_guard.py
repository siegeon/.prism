"""The shared dispatch guard (task ab9166d5 incident, 2026-09-10).

Measured live: task ab9166d5 was parked `blocked` by resume_actuator's own
ceiling ("12 dispatches ... at the ceiling of 12"), yet fresh `claude -p`
children kept being spawned for it minutes apart. resume_actuator counted
its OWN attempts (including deferred no-ops, before the claim check), and
task_runner's real dispatches -- the ones that actually ran the GPU for up
to 1800s each -- were never counted by anyone. Nothing re-checked task
status at the one moment that matters: immediately before the expensive
call.

This pins `prism_service.services.dispatch_guard` as the one chokepoint
that closes both gaps:
  AC-1 -- a task not `in_progress` is refused, with a visible history row,
          however it got to try_begin.
  AC-2 -- a real dispatch writes exactly one attributable history row and
          returns a ticket the caller may proceed with.
  AC-3 -- the ceiling is SHARED: dispatches from two different seat names
          count against the SAME total, so the counter cannot disagree
          with what actually ran the way resume_actuator's own count did.
  AC-4 -- past the ceiling, the task is parked blocked with an explicit
          reason and refused, never invoked again.
  AC-5 -- release() lifts a park this module made and resets the count;
          it never touches a park it did not make.
  AC-6 -- a DispatchTicket re-beats drive_heartbeat while open, so a step
          that outlives HEARTBEAT_WINDOW_S still reads 'driving', not
          'stalled' -- the exact gap that hid this incident.
  AC-7 -- the reaper kills a daemon-spawned `claude -p` child whose task
          has left a driving state, and leaves an `in_progress` sibling's
          child alone (the inverse guard).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import pytest


def _project() -> str:
    return "dispatch-guard-" + uuid.uuid4().hex[:8]


@pytest.fixture()
def make_task():
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(project, **kwargs):
        ctx = get_project(project)
        task = ctx.task_svc.create(
            title=kwargs.pop("title", "dispatch guard task"), **kwargs)
        ctx.task_svc.update(task.id, status="in_progress",
                            workflow_step="implement_tasks")
        created.append(task.id)
        return ctx, ctx.task_svc.get(task.id)

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


# ---------------------------------------------------------------------------
# AC-1 -- refuse a task that is not in a driving state
# ---------------------------------------------------------------------------

def test_refuses_a_blocked_task(make_task):
    from prism_service.services import dispatch_guard as dg

    project = _project()
    ctx, task = make_task(project)
    ctx.task_svc.update(task.id, status="blocked",
                        blocked_reason="parked for some other reason")

    ticket, reason = dg.try_begin(project, task.id, "implement_tasks",
                                  "some-seat")

    assert ticket is None, "a blocked task must never be dispatched"
    assert "blocked" in (reason or "")

    refused = [r for r in ctx.task_svc.history(task.id)
              if r.action == dg.REFUSED_ACTION]
    assert len(refused) == 1, (
        f"a refusal must be visible on the task's own history; got "
        f"{[r.action for r in ctx.task_svc.history(task.id)]}")


def test_refuses_a_done_task(make_task):
    from prism_service.services import dispatch_guard as dg

    project = _project()
    ctx, task = make_task(project)
    ctx.task_svc.update(task.id, status="done")

    ticket, reason = dg.try_begin(project, task.id, "implement_tasks", "seat")
    assert ticket is None
    assert "done" in (reason or "")


def test_allows_an_in_progress_task(make_task):
    from prism_service.services import dispatch_guard as dg

    project = _project()
    ctx, task = make_task(project)

    ticket, reason = dg.try_begin(project, task.id, "implement_tasks", "seat")
    try:
        assert ticket is not None, reason
        assert reason is None
    finally:
        dg.end_dispatch(ticket)


def test_flow_start_never_resurrects_a_governance_park(make_task):
    """The deeper half of the same incident: `flow_start` (the entry point
    every dispatcher -- task_runner, resume_actuator, and MCP conductor_work
    -- calls before it ever reaches try_begin) used to flip ANY blocked task
    straight back to in_progress via its own `_mark_in_progress`, before
    try_begin's status check ever ran. A governance park must survive a
    flow_start call untouched."""
    from prism_service.api import conductor_flow as cf
    from prism_service.services import dispatch_guard as dg

    project = _project()
    ctx, task = make_task(project)
    ctx.task_svc.update(
        task.id, status="blocked",
        blocked_reason="dispatch-guard: 12 dispatches for this task, at "
                       "the ceiling of 12. Refusing to dispatch again -- "
                       "parked for a person.")

    cf.flow_start(cf.Ident(task_id=task.id, session_id="some-seat"),
                  project=project)

    refreshed = ctx.task_svc.get(task.id)
    assert refreshed.status == "blocked", (
        "flow_start must never silently un-park a task a governance seat "
        f"blocked; got status={refreshed.status!r}")
    assert dg.is_governance_park(refreshed.blocked_reason)


def test_flow_start_still_resurrects_an_ordinary_block(make_task):
    """The carve-out is narrow -- an owner block, or an ordinary
    dependency wait, is untouched by this change and keeps the existing,
    already-relied-on behavior (task e4c631d7)."""
    from prism_service.api import conductor_flow as cf

    project = _project()
    ctx, task = make_task(project)
    ctx.task_svc.update(task.id, status="blocked",
                        blocked_reason="waiting on a dependency")

    cf.flow_start(cf.Ident(task_id=task.id, session_id="some-seat"),
                  project=project)

    assert ctx.task_svc.get(task.id).status == "in_progress"


# ---------------------------------------------------------------------------
# AC-2 / AC-3 / AC-4 -- one shared, authoritative ceiling
# ---------------------------------------------------------------------------

def test_dispatch_writes_one_history_row_per_real_call(make_task):
    from prism_service.services import dispatch_guard as dg

    project = _project()
    ctx, task = make_task(project)

    ticket, _ = dg.try_begin(project, task.id, "implement_tasks", "task-runner")
    dg.end_dispatch(ticket)

    rows = [r for r in ctx.task_svc.history(task.id) if r.action == dg.DISPATCH_ACTION]
    assert len(rows) == 1, rows
    assert rows[0].actor == "task-runner"


def test_ceiling_is_shared_across_different_seats(make_task, monkeypatch):
    """The exact defect: resume_actuator counted only ITS OWN attempts, so
    task_runner's real dispatches never touched the ceiling. Two different
    seat names must count against the SAME total."""
    from prism_service.services import dispatch_guard as dg

    monkeypatch.setenv("PRISM_DISPATCH_CEILING", "3")
    project = _project()
    ctx, task = make_task(project)

    seats = ["prism-task-runner", "prism-resume-actuator", "prism-task-runner"]
    for seat in seats:
        ticket, reason = dg.try_begin(project, task.id, "implement_tasks", seat)
        assert ticket is not None, (seat, reason)
        dg.end_dispatch(ticket)

    # The 4th dispatch, from EITHER seat, must trip the shared ceiling.
    ticket, reason = dg.try_begin(project, task.id, "implement_tasks",
                                  "prism-resume-actuator")
    assert ticket is None, (
        "a 4th real dispatch must be refused once 3 dispatches -- from "
        "either seat -- have already run against a ceiling of 3")
    assert "ceiling" in (reason or "")

    task = ctx.task_svc.get(task.id)
    assert task.status == "blocked", (
        f"a ceiling refusal must park the task; got status={task.status!r}")
    assert "3" in (task.blocked_reason or "")

    parked = [r for r in ctx.task_svc.history(task.id) if r.action == dg.PARKED_ACTION]
    assert len(parked) == 1, parked


def test_a_deferred_no_dispatch_never_counts_against_the_ceiling(make_task):
    """Reproduces the exact accounting bug: resume_actuator used to write
    its dispatch history row BEFORE checking whether the claim was even
    available, so a deferral (another driver already holds it) still
    spent budget. dispatch_guard is only ever called once a real invoke
    is about to happen -- a caller that never calls try_begin for a
    deferral spends nothing, by construction."""
    from prism_service.services import dispatch_guard as dg

    project = _project()
    ctx, task = make_task(project)

    # A caller that defers (e.g. loses a claim race) simply never calls
    # try_begin -- there is nothing to "undo" because nothing was counted.
    assert dg._total_dispatches(ctx.task_svc, task.id) == 0


# ---------------------------------------------------------------------------
# AC-5 -- release
# ---------------------------------------------------------------------------

def test_release_unparks_and_resets_the_count(make_task, monkeypatch):
    from prism_service.services import dispatch_guard as dg

    monkeypatch.setenv("PRISM_DISPATCH_CEILING", "1")
    project = _project()
    ctx, task = make_task(project)

    ticket, _ = dg.try_begin(project, task.id, "implement_tasks", "seat")
    dg.end_dispatch(ticket)
    # Ceiling of 1 already spent -- next call parks.
    ticket, reason = dg.try_begin(project, task.id, "implement_tasks", "seat")
    assert ticket is None
    assert ctx.task_svc.get(task.id).status == "blocked"

    result = dg.release(project, task.id, actor="human")
    assert result["ok"] is True
    assert result["unparked"] is True

    task = ctx.task_svc.get(task.id)
    assert task.status == "in_progress"
    assert task.blocked_reason == ""
    assert dg._total_dispatches(ctx.task_svc, task.id) == 0, (
        "release must reset the count -- otherwise the very next dispatch "
        "re-trips the same ceiling immediately")

    # And a fresh dispatch now succeeds again.
    ticket, reason = dg.try_begin(project, task.id, "implement_tasks", "seat")
    assert ticket is not None, reason
    dg.end_dispatch(ticket)


def test_release_never_touches_a_park_it_did_not_make(make_task):
    from prism_service.services import dispatch_guard as dg

    project = _project()
    ctx, task = make_task(project)
    ctx.task_svc.update(task.id, status="blocked",
                        blocked_reason="resume-actuator: something unrelated")

    result = dg.release(project, task.id, actor="human")
    assert result["unparked"] is False

    task = ctx.task_svc.get(task.id)
    assert task.status == "blocked", (
        "release must only lift a park THIS module made -- a park made by "
        "a different seat's own bookkeeping is left untouched")


# ---------------------------------------------------------------------------
# AC-6 -- the heartbeat, the visibility fix
# ---------------------------------------------------------------------------

def test_ticket_rebeats_drive_heartbeat_while_open(make_task, monkeypatch):
    from prism_service.services import dispatch_guard as dg
    from prism_service.services import drive_heartbeat

    monkeypatch.setattr(dg, "HEARTBEAT_INTERVAL_S", 0.05)

    project = _project()
    ctx, task = make_task(project)
    scores_db = dg._scores_db_for(project)

    ticket, _ = dg.try_begin(project, task.id, "implement_tasks", "seat")
    try:
        time.sleep(0.2)
        beat = drive_heartbeat.latest(scores_db, task.id)
        assert beat is not None, (
            "a long-running dispatch must keep beating drive_heartbeat -- "
            "neither existing producer (implement.js prompt discipline, "
            "drive_activity_observer's transcript watch) can see an "
            "internal --no-session-persistence invoke")
        assert beat["age_s"] < 1.0, beat
    finally:
        dg.end_dispatch(ticket)


def test_ticket_stops_beating_once_closed(make_task, monkeypatch):
    from prism_service.services import dispatch_guard as dg

    monkeypatch.setattr(dg, "HEARTBEAT_INTERVAL_S", 0.05)
    project = _project()
    ctx, task = make_task(project)

    ticket, _ = dg.try_begin(project, task.id, "implement_tasks", "seat")
    dg.end_dispatch(ticket)
    assert ticket._thread is not None
    ticket._thread.join(timeout=1.0)
    assert not ticket._thread.is_alive(), (
        "a closed ticket must not keep beating in the background")


# ---------------------------------------------------------------------------
# AC-7 -- the reaper
# ---------------------------------------------------------------------------

_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the reaper reads /proc + ps, Linux-only (true everywhere this "
           "daemon runs: WSL dev and the AOS host alike)")


@pytest.fixture()
def fake_claude_child(tmp_path):
    """A real, short-lived process whose ps() line looks exactly like a
    daemon-spawned `claude -p ...` child (argv[0] == 'claude', per
    claude_cli._build_cmd), with its cwd inside a fake
    task_workspaces/<task_id> directory -- the same signature
    dispatch_guard's reaper looks for. Cleans itself up even if a test
    fails to kill it.

    Built as a real compiled binary, NOT a `#!` shebang script: a script
    is exec'd by the KERNEL's binfmt_script handler, which rewrites argv
    to [interpreter, script_path, ...args] -- ps then shows
    "/bin/sh /path/to/claude -p ..." instead of "claude -p ...", which
    would silently make this fixture untrue to the real `claude` CLI (a
    real ELF binary, confirmed live: `file $(which claude)` -> ELF, not a
    script). Only a real binary preserves argv[0] exactly as passed."""
    import shutil

    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        pytest.skip("no C compiler available to build a faithful test binary")
    src = tmp_path / "claude.c"
    src.write_text("#include <unistd.h>\nint main(void) { sleep(120); return 0; }\n")
    binary = tmp_path / "claude"
    built = subprocess.run([cc, "-O0", "-o", str(binary), str(src)],
                           capture_output=True, text=True)
    if built.returncode != 0:
        pytest.skip(f"could not compile test claude binary: {built.stderr}")

    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"

    spawned: list[subprocess.Popen] = []

    def _spawn(task_id: str):
        workspace = tmp_path / "task_workspaces" / task_id
        workspace.mkdir(parents=True)
        proc = subprocess.Popen(["claude", "-p", "dummy prompt for a test"],
                                cwd=str(workspace), env=env)
        spawned.append(proc)
        time.sleep(0.2)  # let it actually get scheduled and settle into cwd
        return proc

    yield _spawn

    for proc in spawned:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


@_LINUX_ONLY
def test_reaper_kills_a_child_of_a_blocked_task(make_task, fake_claude_child):
    from prism_service.services import dispatch_guard as dg

    project = _project()
    ctx, task = make_task(project)
    ctx.task_svc.update(task.id, status="blocked", blocked_reason="parked")

    proc = fake_claude_child(task.id)
    assert proc.poll() is None, "the fake child must be alive to start"

    found = dg._find_daemon_children()
    assert any(r["pid"] == proc.pid for r in found), (
        f"the reaper must discover its own test child by ppid+cmdline; "
        f"found={found}")
    assert dg._task_id_from_pid(proc.pid) == task.id

    results = dg.sweep_reap()
    matching = [r for r in results if r["pid"] == proc.pid]
    assert matching, f"the reaper must act on {proc.pid}; results={results}"
    assert matching[0]["status"] == "blocked"

    proc.wait(timeout=10)
    assert proc.poll() is not None, "the reaped child must actually be dead"

    reaped_rows = [r for r in ctx.task_svc.history(task.id)
                  if r.action == dg.REAPED_ACTION]
    assert len(reaped_rows) == 1, (
        "a reap must be visible on the task's own history, with why and "
        f"how long it ran; got {reaped_rows}")
    assert str(proc.pid) in reaped_rows[0].details


@_LINUX_ONLY
def test_reaper_never_touches_a_child_of_an_in_progress_task(make_task, fake_claude_child):
    """The inverse guard: a task genuinely in_progress must still be able
    to run its child to completion -- a reaper that cannot tell the
    difference is as dangerous as no reaper at all."""
    from prism_service.services import dispatch_guard as dg

    project = _project()
    ctx, task = make_task(project)
    assert ctx.task_svc.get(task.id).status == "in_progress"

    proc = fake_claude_child(task.id)

    results = dg.sweep_reap()
    matching = [r for r in results if r["pid"] == proc.pid]
    assert not matching, (
        f"the reaper must never touch a child of an in_progress task; "
        f"acted on it anyway: {matching}")
    assert proc.poll() is None, (
        "the in_progress task's child must still be alive after a sweep")
