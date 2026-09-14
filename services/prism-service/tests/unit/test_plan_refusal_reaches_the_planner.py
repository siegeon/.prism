"""The plan_gate refusal reaches the planner (task a65c66e5, 2026-09-14).

Live shape that motivated this: plan_gate's form tooth rewound a65c66e5 to
verify_plan twice ("plan_checks: 3 of 3 AC(s) carry no `oracle:` line:
AC-123, AC-456, AC-789"), and verify-plan-loop.json re-ran the IDENTICAL
static prompt each time -- it never asked for an oracle line and never
carried the refusal, so the third attempt would have escalated a form
defect to the owner after all.

Pins:
- AC-1  plan_refusal_block frames a plan_gate refusal and is "" otherwise.
- AC-2  /steps/plan-refusal-recall reads task.gate_reason and never raises.
- AC-3  verify-plan-loop.json runs `recall` BEFORE `loop`, and the loop
        prompt interpolates ${refusalBlock} and demands an `oracle:` line.
- AC-4  the runner maps the route and the base variables carry refusalBlock
        (so the no-chain fallback never ships the literal placeholder).
- AC-5  plan.refusal_recall is a registered block.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from prism_service.api import workflows as wf
from prism_service.services import task_runner

_NODE = (Path(__file__).resolve().parents[4]
         / ".prism" / "behaviors" / "conductor" / "verify-plan-loop.json")

_LIVE_REASON = ("Rewind 2/3: plan_gate rubric refused, plan_checks: 3 of 3 "
                "AC(s) carry no `oracle:` line: AC-123, AC-456, AC-789")


# AC-1 ------------------------------------------------------------------
def test_a_plan_gate_refusal_is_framed_for_the_planner():
    block = wf.plan_refusal_block(_LIVE_REASON)
    assert "REFUSED BY plan_gate" in block
    assert "AC-123, AC-456, AC-789" in block
    assert "oracle:" in block


def test_a_non_plan_reason_yields_no_block():
    assert wf.plan_refusal_block("") == ""
    assert wf.plan_refusal_block(
        "green_gate: oracle not evidenced - latest receipt FAILED") == ""


# AC-2 ------------------------------------------------------------------
def test_the_step_reads_the_tasks_gate_reason(monkeypatch):
    task = SimpleNamespace(gate_reason=_LIVE_REASON)
    ctx = SimpleNamespace(task_svc=SimpleNamespace(get=lambda tid: task))
    monkeypatch.setattr(wf, "get_project", lambda project: ctx)
    resp = wf.workflow_step_plan_refusal_recall(
        wf.PlanRefusalRecallRequest(task_id="a65c66e5"), project="prism")
    assert resp.refusal_reason == _LIVE_REASON
    assert "AC-123" in resp.refusal_block


def test_the_step_never_raises(monkeypatch):
    def _boom(project):
        raise RuntimeError("no project")
    monkeypatch.setattr(wf, "get_project", _boom)
    resp = wf.workflow_step_plan_refusal_recall(
        wf.PlanRefusalRecallRequest(task_id="x"), project="prism")
    assert resp.refusal_block == "" and resp.refusal_reason == ""


# AC-3 ------------------------------------------------------------------
def test_the_node_recalls_before_it_plans():
    node = json.loads(_NODE.read_text(encoding="utf-8"))
    ids = [s["id"] for s in node["steps"]]
    assert ids.index("recall") < ids.index("loop")
    recall = next(s for s in node["steps"] if s["id"] == "recall")
    assert "/api/workflows/steps/plan-refusal-recall" in recall["url"]
    assert "${taskId}" in recall["body"]


def test_the_planner_prompt_carries_the_refusal_and_demands_an_oracle():
    node = json.loads(_NODE.read_text(encoding="utf-8"))
    loop = next(s for s in node["steps"] if s["id"] == "loop")
    prompt = json.loads(
        loop["body"].replace("${refusalBlock}", "R")
        .replace("${taskHint}", "T").replace("${taskId}", "I"))["prompt"]
    assert prompt.startswith("R"), "the refusal block leads the prompt"
    assert "- oracle:" in prompt, "the per-AC oracle shape is demanded"
    assert "${refusalBlock}" in loop["body"]


# AC-4 ------------------------------------------------------------------
def test_the_runner_maps_the_route_and_the_base_variables_carry_the_block():
    assert "plan-refusal-recall" in task_runner._step_handlers()
    task = SimpleNamespace(title="t", description="d", gate_reason=_LIVE_REASON)
    variables = task_runner._build_step_variables(task, "a65c66e5", "prism")
    assert "AC-123" in variables["refusalBlock"]
    clean = SimpleNamespace(title="t", description="d", gate_reason="")
    assert task_runner._build_step_variables(
        clean, "id", "prism")["refusalBlock"] == ""


# AC-5 ------------------------------------------------------------------
def test_plan_refusal_recall_is_a_registered_block():
    from prism_service import blocks as _blocks
    import prism_service.blocks.plan_blocks  # noqa: F401  registers on import
    assert _blocks.get_block("plan.refusal_recall") is not None
