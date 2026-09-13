"""write_failing_tests must not advance on a proof with no test artifact.

Live evidence, task bb3d1f6a: five attempts of `claude_cli.invoke` on the
FULL BRIEF fallback (no declared chain ran, so `_DRAFT_ONLY_WITHOUT_CHAIN`'s
narrow-prompt guard never engaged) each returned exit_code=0 with a
non-empty `final_text()` of exactly an empty `<think></think>` wrapper plus
chat prose ("I have examined the test files and found...") and NO test ever
written to disk. `_run_one_step` treated that as `outcome="pass"` because its
only success check was `proof and exit_code == 0`, so the step advanced to
red_gate, conductor_service minted a `red_step_sha` anchor at a commit with
no tests, and red_gate then refused forever: "the pinned suite passes there,
so a red demonstration can never be produced from this history."

These tests pin `_run_one_step`'s decision at the point it chooses
`outcome`, using the same `_drive_once`-style harness as
test_task_runner_honors_declared_node_plan.py: fake `flow_start`/`flow_report`
so no real conductor state is needed, `_node_plan` forced to None so the
fallback (full-brief, BUILD_TOOLS) path runs -- exactly the path the live
incident hit.
"""
from __future__ import annotations

import uuid

import pytest


class _Result:
    def __init__(self, text: str, exit_code: int = 0):
        self._text = text
        self.exit_code = exit_code
        self.run_id = "r-live"
        self.usage = {"input_tokens": 10, "output_tokens": 5,
                      "cost_usd": 0.01, "model": "haiku"}

    def final_text(self) -> str:
        return self._text

    def graceful_budget_stop(self) -> bool:
        return False


_EMPTY_THINK_PROOF = (
    "<think>\n\n</think>\n\nI have examined the test files and found the "
    "relevant module. The existing tests already cover this case.")

_REAL_TEST_PROOF = (
    "Wrote tests/unit/test_new_thing.py (412 bytes) into the task worktree.\n"
    "def test_new_thing_is_currently_broken():\n"
    "    assert new_thing() == 42\n\n"
    "Ran the pinned suite tests/unit/test_new_thing.py: pytest exit code 1.\n"
    "Committed tests only as a1b2c3d4e5f6: tests/unit/test_new_thing.py")


def _drive_once(monkeypatch, proof_text, exit_code=0):
    from prism_service.api import conductor_flow as flow
    from prism_service.inference import claude_cli
    from prism_service.services import task_runner, task_workspace
    from prism_service.project_context import get_project

    seen: dict = {}

    monkeypatch.setattr(flow, "flow_start", lambda *a, **k: {
        "ok": True, "job": {"step": "write_failing_tests", "kind": "agent",
                            "instructions": "WRITE A FAILING TEST"}})

    def _fake_report(body, project=None):
        seen["outcome"] = body.outcome
        ok = body.outcome == "pass"
        return {"ok": ok, "advanced": ok, "step": "write_failing_tests"}

    monkeypatch.setattr(flow, "flow_report", _fake_report)
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"path": "/tmp"})
    monkeypatch.setattr(task_runner, "_stall_count", lambda *a, **k: 0)
    monkeypatch.setattr(task_runner, "_route_proof", lambda *a, **k: None)
    # Force the fallback (full-brief, BUILD_TOOLS) path -- no declared chain,
    # no narrow prompt -- which is the path the live incident actually hit.
    monkeypatch.setattr(task_runner, "_node_plan", lambda *a, **k: None)
    monkeypatch.setattr(
        task_runner, "_codified_red_test_ids", lambda *a, **k: ([], "n/a"))

    monkeypatch.setattr(
        claude_cli, "invoke",
        lambda prompt, **kw: _Result(proof_text, exit_code=exit_code))

    project = "write-failing-tests-artifact-" + uuid.uuid4().hex[:8]
    task_svc = get_project(project).task_svc
    task = task_svc.create(title="a task mid-drive at write_failing_tests")
    task_svc.update(task.id, status="in_progress")

    res = task_runner._run_one_step(project, task.id)
    return res, seen


def test_an_empty_think_wrapper_with_no_test_does_not_advance(monkeypatch):
    res, seen = _drive_once(monkeypatch, _EMPTY_THINK_PROOF)

    assert seen["outcome"] != "pass", (
        f"a proof with no def test_... artifact must not report success, "
        f"got outcome={seen['outcome']!r}")
    assert res["ok"] is False, res


def test_a_proof_carrying_a_real_test_still_advances(monkeypatch):
    res, seen = _drive_once(monkeypatch, _REAL_TEST_PROOF)

    assert seen["outcome"] == "pass", (
        f"a proof that names a real test and file must advance exactly as "
        f"today, got outcome={seen['outcome']!r}")
    assert res["ok"] is True, res
