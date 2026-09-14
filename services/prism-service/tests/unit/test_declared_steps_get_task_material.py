"""Declared nodes get the task's real material, not just title+description.

WHAT WAS WRONG (verified live on task bb3d1f6a). `_dispatch_declared_steps`
was called with exactly `{"taskHint": ..., "taskId": ..., "project": ...}`
(task_runner.py ~2144), so no node file could ever ask for the pytest ids
`task.verify` names, the `oracle`, `allowed_files`, `stop_if` or `plan_doc`.
write-failing-tests-loop.json's prompt told the model "draft a failing test
for <title+description>" and never showed it the exact pytest node ids
red_gate demands -- so every attempt returned empty and red_gate refused
with rc=4 "could not collect".

THE FIX. `_build_step_variables(task, task_id, project)` folds every field a
node might need into one dict, always as a string (never the literal
"None" for a missing field), and the call site uses it instead of a
hand-written three-key literal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_service.services import task_runner

_REPO_BEHAVIORS = (Path(__file__).resolve().parents[4]
                   / ".prism" / "behaviors" / "conductor")


@pytest.fixture(autouse=True)
def _hermetic_behavior_dir(monkeypatch):
    monkeypatch.setattr(
        task_runner, "_behavior_dir", lambda project: _REPO_BEHAVIORS)


class _Task:
    title = "Fix the thing"
    description = "Make it work"
    oracle = "the test passes"
    verify = ["tests/unit/test_x.py::test_a", "tests/unit/test_x.py::test_b"]
    allowed_files = ["services/prism-service/prism_service/foo.py"]
    stop_if = ["touches a POLICY_FILES entry"]
    plan_doc = "## Plan\nDo the thing."


class _BareTask:
    """A task with none of the optional fields populated."""
    title = "Bare task"
    description = ""
    oracle = ""
    verify: list[str] = []
    allowed_files: list[str] = []
    stop_if: list[str] = []
    plan_doc = ""


def test_build_step_variables_carries_the_pinned_pytest_ids():
    variables = task_runner._build_step_variables(_Task(), "abc123", "prism")
    assert "tests/unit/test_x.py::test_a" in variables["verify"]
    assert "tests/unit/test_x.py::test_b" in variables["verify"]
    assert variables["oracle"] == "the test passes"
    assert "foo.py" in variables["allowedFiles"]
    assert "POLICY_FILES" in variables["stopIf"]
    assert "Do the thing" in variables["planDoc"]
    assert variables["title"] == "Fix the thing"
    # Existing keys must be untouched so every current node keeps working.
    assert variables["taskHint"] == "Fix the thing\n\nMake it work"
    assert variables["taskId"] == "abc123"
    assert variables["project"] == "prism"


def test_build_step_variables_missing_fields_are_empty_not_the_word_none():
    variables = task_runner._build_step_variables(_BareTask(), "id2", "prism")
    for key in ("verify", "oracle", "allowedFiles", "stopIf", "planDoc"):
        assert variables[key] == "", (
            f"{key} should interpolate as '' for a missing field, "
            f"got {variables[key]!r}")
        assert "None" not in variables[key]


def test_write_failing_tests_prompt_shows_the_exact_pytest_ids():
    """Integration: run the real declared chain and check the reason-loop
    prompt the model actually sees names the pinned test ids, not just the
    task's title+description."""
    captured: dict = {}

    def _draft(project, body):
        captured.update(body)
        class _Result:
            reason = {"fields": {"test_file_path": "tests/unit/test_x.py",
                                 "test_code": "def test_a():\n    assert False"}}
        return _Result()

    def _capture(project, body):
        return {"outcome": "ok", "written": True}

    def _real_targets(project, body):
        from prism_service.api import workflows as _wf
        return _wf.workflow_step_red_targets_from_acs(
            _wf.RedTargetsRequest(**body), project=project)

    def _real_compose(project, body):
        from prism_service.api import workflows as _wf
        return _wf.workflow_step_red_prompt_compose(
            _wf.RedPromptComposeRequest(**body), project=project)

    plan = task_runner._node_plan("prism", "write_failing_tests")
    assert plan is not None
    handlers = {s["route"]: _capture for s in plan["steps"]}
    handlers["reason-loop"] = _draft
    # These two are REAL (not _capture) because the pinned test ids and
    # oracle reach the loop's prompt only through them now -- targets/
    # compose thread ${verify}/${planDoc}/${oracle} directly (no DB
    # fetch needed, per RedTargetsRequest/RedPromptComposeRequest's own
    # docstrings), so this test needs no task_id-backed project fixture.
    handlers["red-targets-from-acs"] = _real_targets
    handlers["red-prompt-compose"] = _real_compose

    variables = task_runner._build_step_variables(_Task(), "abc123", "prism")
    task_runner._dispatch_declared_steps(
        "prism", plan, handlers=handlers, variables=variables)

    prompt = captured.get("prompt", "")
    assert prompt, "reason-loop got no prompt at all"
    assert "tests/unit/test_x.py::test_a" in prompt, (
        "the model was never shown the pytest id it must produce; "
        f"prompt was:\n{prompt}")
    assert "the test passes" in prompt, (
        "the model was never shown the task's oracle; "
        f"prompt was:\n{prompt}")


def test_the_fallback_prompt_substitutes_every_variable_not_just_taskhint():
    """_declared_agentic_prompt serves the SAME declared prompt the node file
    carries. That file now asks for ${verify}; a bare ${taskHint} replace
    would hand the model the literal placeholder on the one path taken
    exactly when the declared chain could not run."""
    from prism_service.services import task_runner as tr

    class T:
        id = "t-fallback"
        title = "a task"
        description = "some description"
        verify = ["tests/unit/test_x.py::test_a"]
        oracle = "the pinned suite goes red"
        allowed_files = []
        stop_if = []
        plan_doc = ""

    plan = {"prompt": "Write a test for ${taskHint}. Pinned: ${verify}. "
                      "Oracle: ${oracle}."}
    out = tr._declared_agentic_prompt("write_failing_tests", T(), {}, plan=plan)
    assert "${verify}" not in out, out
    assert "${oracle}" not in out, out
    assert "tests/unit/test_x.py::test_a" in out, out
    assert "the pinned suite goes red" in out, out
