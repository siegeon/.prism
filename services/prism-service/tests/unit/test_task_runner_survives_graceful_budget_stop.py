"""A budget-exhausted-but-COMPLETE step report must still advance the task
(task: task_runner discards valid proof on a graceful budget/turn stop).

Confirmed live on three tasks (85f92e4b, 0e2c82f3, 82cc05ee), all failing at
`review_previous_notes` with `flow_report_failure; outcome={'ok': False,
'reason': 'exit=1, no usable output'}` -- while the captured run logs show
each run produced a complete, well-formed "## Premises" report as
`final_text()`, and the run's own terminal `type=="result"` event carried
`stop_reason:"end_turn"` (the model's own turn finished normally) alongside
`is_error:true, subtype:"error_max_budget_usd"` (a POST-HOC budget check
flagged the run after the fact). `task_runner._run_one_step` treated ANY
non-zero exit_code as "no usable output" and discarded the proof outright.

`ClaudeCliResult.graceful_budget_stop()` (inference/claude_cli.py) names the
condition; these tests pin `_run_one_step`'s consumption of it:

  * a graceful budget stop WITH a non-empty final_text advances the step;
  * a graceful budget stop with EMPTY final_text still fails (nothing to
    route);
  * a non-graceful exit=1 (crash, no result event at all) still fails
    exactly as before -- this fix narrows the failure case, it does not
    loosen it generally.
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
        project = "budget-stop-" + uuid.uuid4().hex[:8]
        ctx = get_project(project)
        task = ctx.task_svc.create(title=kwargs.pop("title", "budget task"),
                                   **kwargs)
        created.append(task.id)
        return ctx, task, project

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


class _FakeResult:
    """A ClaudeCliResult stand-in carrying exactly the fields
    `_run_one_step` reads: exit_code, final_text(), graceful_budget_stop(),
    usage, run_id."""

    def __init__(self, text: str, exit_code: int, graceful: bool,
                 usage: dict | None = None, run_id: str = "run-budget"):
        self._text = text
        self.exit_code = exit_code
        self._graceful = graceful
        self.usage = dict(usage or {})
        self.run_id = run_id

    def final_text(self) -> str:
        return self._text

    def graceful_budget_stop(self) -> bool:
        return self._graceful


_REPORT = "## Premises\n- confirmed the failure mode with real logs - UNVERIFIED\n"


def _start(ctx, task, project):
    from prism_service.api import conductor_flow as cf

    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                  project=project)
    ctx.task_svc.update(task.id, status="in_progress")


def test_graceful_budget_stop_with_complete_report_advances_the_step(
        make_task, monkeypatch):
    """The exact live failure: exit_code=1 (post-hoc budget flag) but a
    complete final message -- must still route the proof and pass."""
    from prism_service.inference import claude_cli
    from prism_service.services import task_runner as tr

    ctx, task, project = make_task()
    _start(ctx, task, project)

    monkeypatch.setattr(
        claude_cli, "invoke",
        lambda prompt, **kw: _FakeResult(_REPORT, exit_code=1, graceful=True))

    res = tr.run_one_step(project, task.id)

    assert res["ok"] is True, res
    updated = ctx.task_svc.get(task.id)
    assert updated.premise_notes == _REPORT.strip(), (
        "the proof from a graceful budget stop must reach the field its "
        f"gate rubric reads; got {updated.premise_notes!r}")
    # The step actually advanced past review_previous_notes.
    assert updated.workflow_step != "review_previous_notes", updated.workflow_step


def test_graceful_budget_stop_with_empty_text_still_fails(make_task, monkeypatch):
    """A graceful stop with nothing to route is still a failure -- there is
    no proof to advance the step with."""
    from prism_service.inference import claude_cli
    from prism_service.services import task_runner as tr

    ctx, task, project = make_task()
    _start(ctx, task, project)

    monkeypatch.setattr(
        claude_cli, "invoke",
        lambda prompt, **kw: _FakeResult("", exit_code=1, graceful=True))

    res = tr.run_one_step(project, task.id)

    assert res["ok"] is False, res
    updated = ctx.task_svc.get(task.id)
    assert updated.workflow_step == "review_previous_notes", (
        "an empty-proof step must not advance")


def test_non_graceful_failure_still_fails_exactly_as_before(make_task, monkeypatch):
    """A crash / auth failure / mid-generation truncation (non-graceful,
    exit!=0) must not be rescued by this fix even when SOME text happens
    to be present -- only a genuinely graceful stop is exempted."""
    from prism_service.inference import claude_cli
    from prism_service.services import task_runner as tr

    ctx, task, project = make_task()
    _start(ctx, task, project)

    monkeypatch.setattr(
        claude_cli, "invoke",
        lambda prompt, **kw: _FakeResult(
            "partial garbage mid-stream", exit_code=1, graceful=False))

    res = tr.run_one_step(project, task.id)

    assert res["ok"] is False, res
    outcome = res.get("report", {}).get("outcome")
    if isinstance(outcome, dict):
        assert "non-graceful" in outcome.get("reason", "")
    updated = ctx.task_svc.get(task.id)
    assert updated.workflow_step == "review_previous_notes", (
        "a non-graceful failure must not advance the step")


def test_successful_exit_zero_still_passes_without_calling_graceful_check(
        make_task, monkeypatch):
    """exit_code==0 keeps working exactly as before, and never even needs
    to call graceful_budget_stop() (short-circuited) -- a lighter fake
    lacking that method must not break the ordinary success path."""
    from prism_service.inference import claude_cli
    from prism_service.services import task_runner as tr

    ctx, task, project = make_task()
    _start(ctx, task, project)

    class _NoGracefulMethod:
        def __init__(self, text):
            self._text = text
            self.exit_code = 0
            self.usage = {}
            self.run_id = "run-ok"

        def final_text(self):
            return self._text

    monkeypatch.setattr(
        claude_cli, "invoke", lambda prompt, **kw: _NoGracefulMethod(_REPORT))

    res = tr.run_one_step(project, task.id)
    assert res["ok"] is True, res
