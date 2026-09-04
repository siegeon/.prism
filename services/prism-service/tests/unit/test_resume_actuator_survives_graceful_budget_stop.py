"""The RETRY seat must honour a graceful budget stop, exactly as the drive
seat does (task: resume_actuator discards valid proof on a graceful stop).

7.13.102 fixed this in `task_runner._run_one_step`
(`ClaudeCliResult.graceful_budget_stop()`): a post-hoc
`--max-budget-usd` / `--max-turns` ceiling raises the exit code AFTER the
model's own turn ended normally, so a COMPLETE step report must still be
routed. `resume_actuator.dispatch_once` — the seat that RETRIES a step
after the drive seat fails — never inherited that fix and kept testing
`result.exit_code == 0` alone.

LIVE REGRESSION this pins: task ce471e06 (2026-09-04) parked at
`write_failing_tests` after three consecutive
`flow_report_failure; outcome={'ok': False, 'reason': 'exit=1, no usable
output'}` rows from `prism-resume-actuator`, spending the retry budget
(3/3) and blocking the task for a human — while each of those runs had
produced a usable report. A defect in the seat that exists to rescue a
stalled drive is doubly expensive: it converts a recoverable step into a
parked task.

The two seats must now agree:
  * a graceful stop WITH a non-empty report advances the step;
  * a graceful stop with an EMPTY report still fails (nothing to route);
  * a non-graceful exit!=0 still fails, and now names itself
    "non-graceful failure" instead of the misleading "no usable output".
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
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(**kwargs):
        project = "ra-budget-" + uuid.uuid4().hex[:8]
        ctx = get_project(project)
        task = ctx.task_svc.create(title=kwargs.pop("title", "retry task"),
                                   **kwargs)
        created.append(task.id)
        return ctx, task, project

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


class _FakeResult:
    """Carries exactly the fields dispatch_once reads."""

    def __init__(self, text: str, exit_code: int, graceful: bool,
                 usage: dict | None = None, run_id: str = "run-ra"):
        self._text = text
        self.exit_code = exit_code
        self._graceful = graceful
        self.usage = dict(usage or {})
        self.run_id = run_id

    def final_text(self) -> str:
        return self._text

    def graceful_budget_stop(self) -> bool:
        return self._graceful


# Byte-identical to the drive-seat suite's report
# (test_task_runner_survives_graceful_budget_stop.py) on purpose: these two
# tests differ ONLY in which seat consumes the result, so the report must
# not be a second variable.
_REPORT = "## Premises\n- confirmed the failure mode with real logs - UNVERIFIED\n"


def _start(ctx, task, project):
    from prism_service.api import conductor_flow as cf

    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                  project=project)
    ctx.task_svc.update(task.id, status="in_progress")


def test_graceful_budget_stop_with_complete_report_advances_the_step(
        make_task, monkeypatch):
    """The exact live failure on ce471e06: exit_code=1 from a post-hoc
    budget flag, complete report — the retry must route it and pass."""
    from prism_service.inference import claude_cli
    from prism_service.services import resume_actuator as ra

    ctx, task, project = make_task()
    _start(ctx, task, project)

    monkeypatch.setattr(
        claude_cli, "invoke",
        lambda prompt, **kw: _FakeResult(_REPORT, exit_code=1, graceful=True))

    res = ra.dispatch_once(project, task.id)

    assert res.get("ok") is True, res
    updated = ctx.task_svc.get(task.id)
    assert updated.premise_notes == _REPORT.strip(), (
        "the retry seat must route a graceful stop's proof to the field its "
        f"rubric reads; got {updated.premise_notes!r}")
    assert updated.workflow_step != "review_previous_notes", (
        "the step must actually advance")


def test_graceful_budget_stop_with_empty_text_still_fails(
        make_task, monkeypatch):
    """Nothing to route is still a failure, and says 'no usable output'."""
    from prism_service.inference import claude_cli
    from prism_service.services import resume_actuator as ra

    ctx, task, project = make_task()
    _start(ctx, task, project)

    monkeypatch.setattr(
        claude_cli, "invoke",
        lambda prompt, **kw: _FakeResult("", exit_code=1, graceful=True))

    res = ra.dispatch_once(project, task.id)

    assert res.get("ok") is not True, res
    updated = ctx.task_svc.get(task.id)
    assert updated.workflow_step == "review_previous_notes", (
        "an empty-proof retry must not advance the step")


def test_non_graceful_failure_still_fails_and_names_itself(
        make_task, monkeypatch):
    """A crash / auth failure / mid-turn truncation is NOT rescued, and the
    reason distinguishes it from an empty report — the old string claimed
    'no usable output' even when a report was present, which is what made
    ce471e06's three identical rows impossible to self-diagnose."""
    from prism_service.inference import claude_cli
    from prism_service.services import resume_actuator as ra

    ctx, task, project = make_task()
    _start(ctx, task, project)

    monkeypatch.setattr(
        claude_cli, "invoke",
        lambda prompt, **kw: _FakeResult(
            "partial garbage mid-stream", exit_code=1, graceful=False))

    res = ra.dispatch_once(project, task.id)

    assert res.get("ok") is not True, res
    updated = ctx.task_svc.get(task.id)
    assert updated.workflow_step == "review_previous_notes", (
        "a non-graceful failure must not advance the step")
    rows = [str(r) for r in (ctx.task_svc.history(task.id) or [])]
    assert any("non-graceful" in r for r in rows), (
        "the failure must name itself non-graceful, not 'no usable output'")


def test_exit_zero_still_passes_without_a_graceful_check(
        make_task, monkeypatch):
    """The ordinary success path is untouched and must not require the
    graceful_budget_stop attribute at all."""
    from prism_service.inference import claude_cli
    from prism_service.services import resume_actuator as ra

    class _Plain:
        exit_code = 0
        usage: dict = {}
        run_id = "run-plain"

        def final_text(self) -> str:
            return _REPORT

    ctx, task, project = make_task()
    _start(ctx, task, project)

    monkeypatch.setattr(claude_cli, "invoke",
                        lambda prompt, **kw: _Plain())

    res = ra.dispatch_once(project, task.id)

    assert res.get("ok") is True, res
