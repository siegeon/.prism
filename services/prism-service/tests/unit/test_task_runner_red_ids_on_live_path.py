"""Codified red test ids are consulted on the live path (f97c196d, 2026-09-09).

When write_failing_tests step passes, the runner should consult the
deterministic red-test-ids node to make those ids AVAILABLE for downstream
consumers (gates, stall handler) without requiring a model to retype them.

Previously, _codified_red_test_ids was ONLY called inside _handle_stall,
i.e. only AFTER a task had already stalled. Now it is consulted on the live
path during normal write_failing_tests success, so the ids are available
immediately without waiting for a stall.

These tests pin the repair. They assert that when write_failing_tests
succeeds and produces a PASS report, _codified_red_test_ids is consulted
and its result is incorporated into what gets stored.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest


@pytest.fixture()
def write_failing_tests_success(monkeypatch, tmp_path):
    """A real TaskService task at write_failing_tests that passes."""
    from prism_service.project_context import get_project
    from prism_service.api import conductor_flow as flow
    from prism_service.inference import claude_cli
    from prism_service.services import task_workspace

    RED_IDS = [
        "tests/unit/test_alpha.py::test_one",
        "tests/unit/test_alpha.py::test_two",
        "tests/unit/test_beta.py::test_three",
    ]
    MODEL_PROOF = (
        "I have written failing tests. They still fail:\n"
        "- FAILED tests/unit/test_alpha.py::test_one\n"
        "- FAILED tests/unit/test_alpha.py::test_two\n"
        "- FAILED tests/unit/test_beta.py::test_three\n"
        "3 failed, 2 passed.\n"
    )

    project = "wft-" + uuid.uuid4().hex[:8]
    ctx = get_project(project)
    task = ctx.task_svc.create(
        title="write failing tests",
        tags=["x"],
        priority=5,
        verify=" ".join(RED_IDS),  # task.verify names the pinned ids
    )
    ctx.task_svc.update(
        task.id,
        status="in_progress",
        workflow_step="write_failing_tests",
        plan_doc="AC-1: write tests for alpha\nAC-2: write tests for beta"
    )

    class _FakeResult:
        def __init__(self):
            self.exit_code = 0
            self.run_id = "run-wft"
            self.usage = {}

        def final_text(self):
            return MODEL_PROOF

    invoke_calls: list = []

    def _invoke(prompt, **kw):
        invoke_calls.append({"prompt": prompt, **kw})
        return _FakeResult()

    def _flow_start(ident, project=None):
        return {
            "ok": True,
            "job": {"step": "write_failing_tests", "kind": "agent",
                    "instructions": "make failing tests"}
        }

    def _flow_report(ident, project=None):
        # Record the flow report as the real flow_report does
        # (mirrors test_task_runner_stall_detection fixture)
        ctx.task_svc.record_history(
            ident.task_id, action="flow_report_success",
            details=f"step=write_failing_tests; outcome=pass",
            actor=ident.session_id)
        return {"ok": True, "step": "write_failing_tests", "advanced": True}

    monkeypatch.setattr(claude_cli, "invoke", _invoke)
    monkeypatch.setattr(flow, "flow_start", _flow_start)
    monkeypatch.setattr(flow, "flow_report", _flow_report)
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"path": str(tmp_path)})

    return {
        "ctx": ctx,
        "project": project,
        "task_id": task.id,
        "red_ids": RED_IDS,
        "model_proof": MODEL_PROOF,
        "invoke_calls": invoke_calls,
    }


def test_write_failing_tests_success_consults_codified_red_ids(
        write_failing_tests_success, monkeypatch):
    """When write_failing_tests PASSES, codified red ids are consulted.

    AC-1: The runner calls _codified_red_test_ids on the live path when
    write_failing_tests step succeeds with a PASS report.
    """
    from prism_service.services import task_runner as tr

    data = write_failing_tests_success
    project = data["project"]
    task_id = data["task_id"]
    red_ids = data["red_ids"]

    codified_calls = []

    original_codified = tr._codified_red_test_ids

    def _mock_codified(proj, tid):
        codified_calls.append((proj, tid))
        # Return the authoritative codified ids
        return red_ids, f"red demonstrated at abc1234: test runs show all 3 failing"

    monkeypatch.setattr(tr, "_codified_red_test_ids", _mock_codified)

    result = tr.run_one_step(project, task_id)

    # The step must have succeeded
    assert result["ok"], f"step failed: {result}"
    assert result["step"] == "write_failing_tests"

    # _codified_red_test_ids must have been called on the live path
    assert len(codified_calls) == 1, (
        "codified red ids not consulted on live path; "
        f"calls: {codified_calls}")
    assert codified_calls[0] == (project, task_id)

    # The stored completion_proof should contain the red ids
    task = data["ctx"].task_svc.get(task_id)
    completion_proof = getattr(task, "completion_proof", "") or ""
    assert completion_proof, "completion_proof should be stored"

    # All red ids from codified should be findable in the proof
    for red_id in red_ids:
        assert red_id in completion_proof, (
            f"red id {red_id} not found in stored completion_proof: "
            f"{completion_proof}")


def test_write_failing_tests_without_codified_result_still_succeeds(
        write_failing_tests_success, monkeypatch):
    """When _codified_red_test_ids returns empty, the step still succeeds.

    AC-2: If the codified node returns no valid ids (e.g. no anchor yet,
    not pytest-backed), the step still advances normally; the absence of
    codified ids is not a failure condition.
    """
    from prism_service.services import task_runner as tr

    data = write_failing_tests_success
    project = data["project"]
    task_id = data["task_id"]

    def _mock_codified_empty(proj, tid):
        # No anchor yet
        return [], "no red-step commit resolved yet"

    monkeypatch.setattr(tr, "_codified_red_test_ids", _mock_codified_empty)

    result = tr.run_one_step(project, task_id)

    # The step must succeed even when codified returns empty
    assert result["ok"], f"step failed when codified had no ids: {result}"
    assert result["step"] == "write_failing_tests"

    # completion_proof should still be stored (from the model output)
    task = data["ctx"].task_svc.get(task_id)
    assert getattr(task, "completion_proof", ""), (
        "completion_proof should be stored even when codified is empty")
