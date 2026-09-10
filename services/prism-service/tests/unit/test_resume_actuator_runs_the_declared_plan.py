"""The resume_actuator seat honors declared node plans (task TBD).

resume_actuator is a SECOND driver with its own invoke call. When task_runner
gained plan-aware helpers (_node_plan, _declared_agentic_prompt, _invoke_budget),
resume_actuator's dispatch was never updated, so it still sends job["instructions"]
(the full 30-turn brief) with the default 2.0 budget, no model, and no timeout_s.

The fix: route through the SAME plan-aware helpers task_runner uses, for both
planned steps (like draft_story) and unplanned steps (like implement_tasks).
"""

from __future__ import annotations

import pytest


def test_draft_story_dispatch_uses_declared_plan(monkeypatch):
    """For draft_story, the declared plan caps override the defaults.

    draft_story declares: haiku, 4 turns, 0.50 USD, no tools, narrow prompt.
    The seat's invoke MUST receive:
    - model: "haiku"
    - max_turns: 4
    - max_budget_usd: 0.50
    - allowed_tools: () (empty tuple, not BUILD_TOOLS)
    - timeout_s: a real value (not None)
    - prompt: containing "## Acceptance Criteria" (declared) not "RULE, before anything else" (full brief)
    """
    from prism_service.services import resume_actuator as ra
    from prism_service.inference import claude_cli
    from prism_service.project_context import get_project

    # Capture invoke kwargs
    captured_kwargs = {}
    def mock_invoke(prompt, **kwargs):
        captured_kwargs.update(kwargs)
        captured_kwargs["prompt"] = prompt
        # Return a mock result
        class MockResult:
            exit_code = 0
            def final_text(self):
                return "## Acceptance Criteria\nAC-1: test"
            def graceful_budget_stop(self):
                return False
        return MockResult()

    monkeypatch.setattr(claude_cli, "invoke", mock_invoke)

    # Mock _declared_agentic_prompt to return the narrow prompt for draft_story
    from prism_service.services import task_runner as ra_tr
    orig_prompt = ra_tr._declared_agentic_prompt
    # `plan=` was added by task a9f2bec7: verify_plan reads its narrow prompt
    # from the DECLARATION rather than from a copy in task_runner, so the
    # declared plan has to travel to the prompt builder. The double mirrors the
    # real signature, which is what makes this test able to see that the seat
    # passes it.
    def mock_declared_prompt(step, task, facts, plan=None):
        # For draft_story, return a narrow prompt (non-empty means narrow=True)
        if step == "draft_story":
            return "## Acceptance Criteria\nWrite..."
        return orig_prompt(step, task, facts, plan=plan)
    monkeypatch.setattr(ra_tr, "_declared_agentic_prompt", mock_declared_prompt)

    # Mock the full flow to get draft_story step
    from prism_service.api import conductor_flow as flow
    from unittest.mock import MagicMock

    def mock_flow_start(ident, project=None):
        return {
            "ok": True,
            "job": {
                "step": "draft_story",
                "kind": "step",
                "instructions": "RULE, before anything else, ...[full 30-turn brief]..."
            }
        }

    monkeypatch.setattr(flow, "flow_start", mock_flow_start)

    # Mock workspace
    from prism_service.services import task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for", lambda tid: {"path": "/tmp/ws"})

    # Mock task_svc methods
    ctx = get_project("prism")
    task_svc = ctx.task_svc
    orig_record = task_svc.record_history
    hist_rows = []
    def mock_record(tid, **kw):
        hist_rows.append(kw)
        return orig_record(tid, **kw)
    monkeypatch.setattr(task_svc, "record_history", mock_record)

    # Mock scores db and heartbeat
    from prism_service.services import drive_heartbeat
    monkeypatch.setattr(drive_heartbeat, "record_heartbeat", lambda *a, **k: None)

    # Dispatch
    result = ra.dispatch_once("prism", "test-task-id")

    # Verify the invoke received declared caps
    assert captured_kwargs.get("model") == "haiku", (
        f"draft_story should use haiku model, got {captured_kwargs.get('model')}")
    assert captured_kwargs.get("max_turns") == 4, (
        f"draft_story should use 4 turns, got {captured_kwargs.get('max_turns')}")
    assert captured_kwargs.get("max_budget_usd") == 0.50, (
        f"draft_story should use 0.50 USD, got {captured_kwargs.get('max_budget_usd')}")
    assert captured_kwargs.get("allowed_tools") == (), (
        f"draft_story should use no tools, got {captured_kwargs.get('allowed_tools')}")
    assert captured_kwargs.get("timeout_s") is not None, (
        "draft_story must have a timeout_s (was None)")

    prompt = captured_kwargs.get("prompt", "")
    assert "## Acceptance Criteria" in prompt, (
        "draft_story prompt should use declared narrow prompt with AC heading")
    assert "RULE, before anything else" not in prompt, (
        "draft_story prompt should NOT use full step brief")


def test_unplanned_step_dispatch_uses_defaults(monkeypatch):
    """For steps with NO declared plan (like implement_tasks), dispatch uses defaults.

    Unplanned steps keep the original behavior:
    - model: "" (default, not overridden)
    - max_turns: _max_turns() (30)
    - max_budget_usd: _max_budget_usd() (2.0)
    - allowed_tools: BUILD_TOOLS
    - timeout_s: a real value (not None) - even unplanned steps need wall clock
    - prompt: job["instructions"] (the full brief)
    """
    from prism_service.services import resume_actuator as ra
    from prism_service.services.task_runner import BUILD_TOOLS, _max_turns, _max_budget_usd
    from prism_service.inference import claude_cli
    from prism_service.project_context import get_project

    captured_kwargs = {}
    def mock_invoke(prompt, **kwargs):
        captured_kwargs.update(kwargs)
        captured_kwargs["prompt"] = prompt
        class MockResult:
            exit_code = 0
            def final_text(self):
                return "test output"
            def graceful_budget_stop(self):
                return False
        return MockResult()

    monkeypatch.setattr(claude_cli, "invoke", mock_invoke)

    from prism_service.api import conductor_flow as flow
    def mock_flow_start(ident, project=None):
        return {
            "ok": True,
            "job": {
                "step": "implement_tasks",
                "kind": "step",
                "instructions": "Implement the following..."
            }
        }
    monkeypatch.setattr(flow, "flow_start", mock_flow_start)

    from prism_service.services import task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for", lambda tid: {"path": "/tmp/ws"})

    ctx = get_project("prism")
    task_svc = ctx.task_svc
    orig_record = task_svc.record_history
    def mock_record(tid, **kw):
        return orig_record(tid, **kw)
    monkeypatch.setattr(task_svc, "record_history", mock_record)

    from prism_service.services import drive_heartbeat
    monkeypatch.setattr(drive_heartbeat, "record_heartbeat", lambda *a, **k: None)

    result = ra.dispatch_once("prism", "test-task-id")

    # Verify defaults are preserved
    assert captured_kwargs.get("max_turns") == _max_turns(), (
        f"unplanned step should use default {_max_turns()} turns")
    assert captured_kwargs.get("max_budget_usd") == _max_budget_usd(), (
        f"unplanned step should use default {_max_budget_usd()} USD")
    assert captured_kwargs.get("allowed_tools") == BUILD_TOOLS, (
        f"unplanned step should use BUILD_TOOLS")
    assert captured_kwargs.get("timeout_s") is not None, (
        "even unplanned steps must have a timeout_s (was None)")

    prompt = captured_kwargs.get("prompt", "")
    assert prompt == "Implement the following..." or "Implement" in prompt, (
        "unplanned step should use job['instructions']")


def test_both_planned_and_unplanned_get_timeout(monkeypatch):
    """Regression: dispatch ALWAYS passes timeout_s, never None.

    An unbounded child can run 20+ minutes. The seat needs a wall clock
    for both planned steps (with declared timeouts) and unplanned steps
    (with runner defaults).
    """
    from prism_service.services import resume_actuator as ra
    from prism_service.inference import claude_cli
    from prism_service.project_context import get_project
    from prism_service.api import conductor_flow as flow
    from prism_service.services import task_workspace, drive_heartbeat

    def test_for_step(step_name):
        captured_kwargs = {}
        def mock_invoke(prompt, **kwargs):
            captured_kwargs.update(kwargs)
            class MockResult:
                exit_code = 0
                def final_text(self):
                    return "output"
                def graceful_budget_stop(self):
                    return False
            return MockResult()

        monkeypatch.setattr(claude_cli, "invoke", mock_invoke)

        def mock_flow_start(ident, project=None):
            return {
                "ok": True,
                "job": {
                    "step": step_name,
                    "kind": "step",
                    "instructions": "instructions..."
                }
            }
        monkeypatch.setattr(flow, "flow_start", mock_flow_start)
        monkeypatch.setattr(task_workspace, "workspace_for", lambda tid: {"path": "/tmp/ws"})

        ctx = get_project("prism")
        task_svc = ctx.task_svc
        orig_record = task_svc.record_history
        monkeypatch.setattr(task_svc, "record_history",
                          lambda tid, **kw: orig_record(tid, **kw))

        monkeypatch.setattr(drive_heartbeat, "record_heartbeat", lambda *a, **k: None)

        result = ra.dispatch_once("prism", f"task-{step_name}")

        timeout = captured_kwargs.get("timeout_s")
        assert timeout is not None, (
            f"{step_name} dispatch passed no timeout_s (None) — unbounded child")
        assert timeout > 0, (
            f"{step_name} timeout_s must be positive, got {timeout}")
        return timeout

    # Test both a planned and unplanned step
    draft_story_timeout = test_for_step("draft_story")
    implement_timeout = test_for_step("implement_tasks")

    # Both should have real timeouts
    assert draft_story_timeout > 0
    assert implement_timeout > 0
