"""A refused `test_drafted` verdict must stop the write_failing_tests chain
BEFORE write-test-file/commit-tests-only run, not after.

WHAT WAS WRONG (task bb3d1f6a). `arc_governance.score_test_drafted` (7.13.306)
correctly refuses a drafted test whose imports do not resolve -- it is
wired into `_score_rubric` and reachable from
`/steps/reason-loop`'s Validate stage. But `ReasonLoopResponse.validation`
was informational only: nothing in the declared chain read it, so
`_dispatch_declared_steps` ran write-test-file, run-pinned-suite and
commit-tests-only regardless, and a draft with `from prism.ontology import
Term` (no such module) was committed as the task's tests-only anchor.

THE FIX. `workflow_step_reason_loop` now sets `stop_chain=True` on its
response when a `rubric` was declared and the verdict refused, carrying the
refusal reason. `_dispatch_declared_steps`'s existing generic early-exit
(`getattr(result, "stop_chain", False)`) already breaks the loop on that --
this closes the other half: the row recorded for the refused step is
marked not-ok with the refusal text, so the retry/stall path has something
actionable instead of a bare "ran as a declared step".

A clean draft (resolvable imports) must still flow through the whole chain
exactly as before -- this must never regress the passing case pinned in
test_write_failing_tests_runs_as_declared_nodes.py.
"""

from __future__ import annotations

import types

import pytest

from prism_service.services import task_runner

_REPO_BEHAVIORS = None


@pytest.fixture(autouse=True)
def _hermetic_behavior_dir(monkeypatch):
    from pathlib import Path
    behaviors = (Path(__file__).resolve().parents[4]
                 / ".prism" / "behaviors" / "conductor")
    monkeypatch.setattr(task_runner, "_behavior_dir", lambda project: behaviors)


def _plan():
    return task_runner._node_plan("prism", "write_failing_tests")


# ----------------------------------------------------------------------
# Level 1: workflow_step_reason_loop must mark a refused verdict stop_chain
# ----------------------------------------------------------------------

def test_reason_loop_stops_the_chain_on_a_refused_test_drafted_verdict(
        tmp_path, monkeypatch):
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        brain_svc=None, memory_svc=None, task_svc=None, workflow_svc=None,
        governance=None))

    class _FakeContextBuilder:
        def __init__(self, **kw):
            pass

        def build(self, persona, story_file):
            return {"conventions": [], "role_card": {"id": persona}}

    monkeypatch.setattr(workflows_api, "ContextBuilder", _FakeContextBuilder)
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))

    bad_test_code = (
        "from prism.ontology import Term\n\n"
        "def test_prose_keeps_the_words_the_author_wrote():\n"
        "    assert Term('x')\n"
    )

    def _fake_invoke(prompt, *, work_dir, plugin_dir, model, max_budget_usd,
                     max_turns, project, purpose, json_schema, **kw):
        return claude_cli.ClaudeCliResult(
            output_path=tmp_path / "run.jsonl", exit_code=0,
            structured_output={"test_code": bad_test_code,
                               "test_file_path": "tests/unit/test_x.py"},
            usage={"cost_usd": 0.0}, run_id="run-1",
        )

    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)

    resp = workflows_api.workflow_step_reason_loop(
        workflows_api.ReasonLoopRequest(
            persona="qa",
            prompt="Draft a failing test.",
            json_schema={"type": "object"},
            rubric="test_drafted",
            task_id="t1",
        ),
        project="prism",
    )

    assert resp.validation["ok"] is False, (
        "the rubric should have refused the unresolvable import")
    assert getattr(resp, "stop_chain", False) is True, (
        "a refused test_drafted verdict must stop the declared chain -- "
        "otherwise write-test-file/commit-tests-only still run on it")
    assert "prism" in resp.validation["reason"]


def test_reason_loop_does_not_stop_the_chain_on_a_passing_verdict(
        tmp_path, monkeypatch):
    """Preserve today's behaviour: a clean draft must still flow through."""
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        brain_svc=None, memory_svc=None, task_svc=None, workflow_svc=None,
        governance=None))

    class _FakeContextBuilder:
        def __init__(self, **kw):
            pass

        def build(self, persona, story_file):
            return {"conventions": [], "role_card": {"id": persona}}

    monkeypatch.setattr(workflows_api, "ContextBuilder", _FakeContextBuilder)
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))

    good_test_code = (
        "def test_prose_keeps_the_words_the_author_wrote():\n"
        "    assert True\n"
    )

    def _fake_invoke(prompt, *, work_dir, plugin_dir, model, max_budget_usd,
                     max_turns, project, purpose, json_schema, **kw):
        return claude_cli.ClaudeCliResult(
            output_path=tmp_path / "run.jsonl", exit_code=0,
            structured_output={"test_code": good_test_code,
                               "test_file_path": "tests/unit/test_x.py"},
            usage={"cost_usd": 0.0}, run_id="run-1",
        )

    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)

    resp = workflows_api.workflow_step_reason_loop(
        workflows_api.ReasonLoopRequest(
            persona="qa",
            prompt="Draft a failing test.",
            json_schema={"type": "object"},
            rubric="test_drafted",
            task_id="t1",
        ),
        project="prism",
    )

    assert resp.validation["ok"] is True
    assert getattr(resp, "stop_chain", False) is False


def test_reason_loop_does_not_stop_the_chain_when_no_rubric_declared(
        tmp_path, monkeypatch):
    """A node with no rubric skips Validate entirely -- must not stop."""
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        brain_svc=None, memory_svc=None, task_svc=None, workflow_svc=None,
        governance=None))

    class _FakeContextBuilder:
        def __init__(self, **kw):
            pass

        def build(self, persona, story_file):
            return {"conventions": [], "role_card": {"id": persona}}

    monkeypatch.setattr(workflows_api, "ContextBuilder", _FakeContextBuilder)
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))

    def _fake_invoke(prompt, *, work_dir, plugin_dir, model, max_budget_usd,
                     max_turns, project, purpose, json_schema, **kw):
        return claude_cli.ClaudeCliResult(
            output_path=tmp_path / "run.jsonl", exit_code=0,
            structured_output={"plan_doc": "## Plan"},
            usage={"cost_usd": 0.0}, run_id="run-1",
        )

    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)

    resp = workflows_api.workflow_step_reason_loop(
        workflows_api.ReasonLoopRequest(
            persona="sm",
            prompt="Write a plan.",
            json_schema={"type": "object"},
            task_id="t1",
        ),
        project="prism",
    )

    assert getattr(resp, "stop_chain", False) is False


# ----------------------------------------------------------------------
# Level 2: _dispatch_declared_steps must honor stop_chain from reason-loop
# and mark that row not-ok with the refusal reason -- and must NOT call
# write-test-file/run-pinned-suite/commit-tests-only after it.
# ----------------------------------------------------------------------

def test_a_refused_draft_never_reaches_write_or_commit():
    plan = _plan()
    assert plan is not None, "write-failing-tests-loop.json must be readable"

    calls: list[str] = []

    class _Refused:
        reason = {"fields": {"test_file_path": "tests/unit/test_x.py",
                             "test_code": "from prism import x\n"}}
        validation = {"ok": False,
                      "reason": "test_drafted: imports unresolvable module(s): prism"}
        stop_chain = True

    def _route_check(project, body):
        calls.append("route-check")
        return {"route": "pytest"}

    # THE GATHER STEP (task ab9166d5's follow-up): a read-only context
    # lookup that runs BEFORE reason-loop, so it must never be lumped in
    # with the build handlers a refusal is supposed to block.
    def _gather(project, body):
        calls.append("gather")
        return {"brain_context": "repo material"}

    # THE RECALL STEP (task 08e666ff): another read-only lookup, between
    # gather and the loop -- same reasoning as gather above.
    def _recall(project, body):
        calls.append("recall")
        return {"refusal_block": ""}

    # THE SCAFFOLD STEP (task 08e666ff's follow-up): a third read-only
    # lookup, between recall and the loop -- same reasoning as gather/
    # recall above, never one of the build trio a refusal must block.
    def _scaffold(project, body):
        calls.append("scaffold")
        return {"scaffold_block": ""}

    def _draft(project, body):
        calls.append("reason-loop")
        return _Refused()

    def _should_not_run(project, body):
        calls.append("build")
        raise AssertionError("this handler must never be called after a refusal")

    handlers = {s["route"]: _should_not_run for s in plan["steps"]}
    handlers["reason-loop"] = _draft
    handlers["oracle-route-check"] = _route_check
    handlers["context-enrich"] = _gather
    handlers["refusal-recall"] = _recall
    handlers["test-scaffold"] = _scaffold

    rows = task_runner._dispatch_declared_steps(
        "prism", plan, handlers=handlers,
        variables={"taskHint": "h", "taskId": "abc123"})

    assert calls == ["route-check", "gather", "recall", "scaffold", "reason-loop"], (
        f"write-test-file/run-pinned-suite/commit-tests-only ran after a "
        f"refused verdict: {calls}")
    refused_row = next(r for r in rows if r.get("route") == "reason-loop")
    assert refused_row.get("ok") is False, (
        "a refused verdict's row must be reported not-ok so the retry/"
        "stall path has something actionable")
    assert "prism" in (refused_row.get("reason") or ""), (
        f"the refusal text must name the unresolvable module: {refused_row!r}")

    result = task_runner._result_from_dispatch(rows)
    assert result is None, (
        "a refused draft must never advance the step -- no red anchor "
        "was ever written or committed")


def test_a_passing_draft_still_flows_through_the_whole_chain():
    """Regression guard: today's behaviour is unchanged for a clean draft."""
    plan = _plan()
    calls: list[str] = []

    class _Passing:
        reason = {"fields": {"test_file_path": "tests/unit/test_x.py",
                             "test_code": "def test_x():\n    assert True\n"}}
        validation = {"ok": True, "reason": "test_drafted: ok"}
        stop_chain = False

    def _route_check(project, body):
        calls.append("route-check")
        return {"route": "pytest"}

    # THE GATHER STEP (task ab9166d5's follow-up): runs before reason-loop,
    # a distinct call from the write/run/commit "build" trio below.
    def _gather(project, body):
        calls.append("gather")
        return {"brain_context": "repo material"}

    # THE RECALL STEP (task 08e666ff): runs between gather and the loop,
    # also distinct from the build trio.
    def _recall(project, body):
        calls.append("recall")
        return {"refusal_block": ""}

    # THE SCAFFOLD STEP (task 08e666ff's follow-up): also runs between
    # recall and the loop, also distinct from the build trio.
    def _scaffold(project, body):
        calls.append("scaffold")
        return {"scaffold_block": ""}

    def _draft(project, body):
        calls.append("reason-loop")
        return _Passing()

    def _capture(project, body):
        calls.append("build")
        return {"outcome": "ok", "written": True, "rc": 1,
                "committed": True, "sha": "deadbeefcafe", "files": ["x"]}

    handlers = {s["route"]: _capture for s in plan["steps"]}
    handlers["reason-loop"] = _draft
    handlers["oracle-route-check"] = _route_check
    handlers["context-enrich"] = _gather
    handlers["refusal-recall"] = _recall
    handlers["test-scaffold"] = _scaffold

    rows = task_runner._dispatch_declared_steps(
        "prism", plan, handlers=handlers,
        variables={"taskHint": "h", "taskId": "abc123"})

    assert calls == ["route-check", "gather", "recall", "scaffold",
                     "reason-loop", "build", "build", "build"], (
        f"a passing draft must still run the full chain: {calls}")
