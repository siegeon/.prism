"""A declared node's timeoutSeconds must actually bound the reason-loop
call it protects, not just sit in the JSON as decoration.

Verified gap (task bb3d1f6a / b2f29d45): `_node_plan` already parses each
step's `timeoutSeconds` into `steps[i]["timeout_s"]`
(task_runner.py:339-340), but `_dispatch_declared_steps` never reads it
before calling the handler, and `workflow_step_reason_loop` never passes a
`timeout_s` to `claude_cli.invoke` -- so a hung reason-loop call runs
unbounded regardless of what the node declares.
"""

from __future__ import annotations

import types

from prism_service.services import task_runner


def test_dispatch_declared_steps_passes_the_declared_timeout_into_the_body():
    """A reason-loop step declaring timeout_s must hand it to the handler
    so the handler can bind it -- today this key is silently dropped."""
    plan = {"steps": [
        {"route": "reason-loop", "body": {"prompt": "x"}, "timeout_s": 45},
    ]}
    seen: dict = {}

    def _capture(project, body):
        seen.update(body)
        return {"ok": True}

    task_runner._dispatch_declared_steps(
        "prism", plan, handlers={"reason-loop": _capture})

    assert seen.get("timeout_s") == 45, (
        f"declared timeout_s never reached the handler body: {seen!r}")


def test_workflow_step_reason_loop_binds_the_declared_timeout(tmp_path, monkeypatch):
    """End to end: a ReasonLoopRequest carrying timeout_s must reach
    claude_cli.invoke's own timeout_s kwarg, which is what actually kills
    a hung `claude -p` subprocess (claude_cli.py:658)."""
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        brain_svc=None, memory_svc=None, task_svc=None, workflow_svc=None, governance=None))

    class _FakeContextBuilder:
        def __init__(self, **kw):
            pass

        def build(self, persona, story_file):
            return {"conventions": [], "role_card": {"id": persona}}

    monkeypatch.setattr(workflows_api, "ContextBuilder", _FakeContextBuilder)
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))

    captured: dict = {}

    def _fake_invoke(prompt, *, work_dir, plugin_dir, model, max_budget_usd,
                     max_turns, project, purpose, json_schema, **kw):
        captured.update(kw)
        return claude_cli.ClaudeCliResult(
            output_path=tmp_path / "run.jsonl", exit_code=0,
            structured_output={"x": "y"},
            usage={"cost_usd": 0.0}, run_id="run-1",
        )

    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)

    workflows_api.workflow_step_reason_loop(
        workflows_api.ReasonLoopRequest(
            persona="qa",
            prompt="Draft something.",
            json_schema={"type": "object", "properties": {"x": {"type": "string"}}},
            task_id="t1",
            timeout_s=45.0,
        ),
        project="prism",
    )

    assert captured.get("timeout_s") == 45.0, (
        f"declared timeout_s never reached claude_cli.invoke: {captured!r}")
