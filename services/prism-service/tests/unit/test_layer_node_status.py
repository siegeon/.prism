"""A drilled-in layer must be able to say what is going on.

Owner 2026-08-29, looking at the plan-gate layer his task was parked on: "I
do not see any steps in what you are showing me with their progress bar like
from conductor ... there is no indication anywhere what the hell is going
on."

He was right. WorkflowsPage only ever derives node state from
`workflowRun.runtime`, and no WorkflowCore run backs a declarative FSM
behaviour, so `activeProgress` is null on every drilled layer and the canvas
draws a dead diagram. The verdicts were never missing -- only unexposed per
node.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.api import workflows as wf  # noqa: E402


def test_the_plan_rubric_is_scored_with_the_key_the_scorer_reads():
    """_score_rubric's plan_coverage branch reads fields["plan_doc"].

    Passing plan_md/story_md instead scored an EMPTY document and produced a
    confident "story carries no AC-<n> ids" on a plan carrying AC-1..AC-5 --
    a false red on the very node this endpoint exists to tell the truth
    about. Caught before shipping only because the verdict looked wrong.
    """
    src = (Path(__file__).resolve().parent.parent.parent
           / "prism_service/api/workflows.py").read_text(encoding="utf-8")
    i = src.index("def workflow_node_status")
    block = src[i:i + 3000]
    assert '"plan_doc": str(getattr(task, "plan_doc"' in block, (
        "the rubric node is scored with a key the scorer does not read, so "
        "it grades an empty document")


def test_a_layer_with_no_per_node_check_says_so_rather_than_claiming_passed():
    """`unknown` must never be dressed up as `passed`."""
    src = (Path(__file__).resolve().parent.parent.parent
           / "prism_service/api/workflows.py").read_text(encoding="utf-8")
    i = src.index("def workflow_node_status")
    block = src[i:i + 3500]
    assert 'state="unknown"' in block
    assert "not_reached" in block, (
        "a node inference never reached must read as not_reached, not as a "
        "pass it never earned")


def test_infer_reads_not_reached_when_the_codified_layer_decided(monkeypatch):
    """The ordering must be visible on the node, not just enforced in code."""
    task = types.SimpleNamespace(plan_doc="", plan_diagram="", id="t1")
    ctx = types.SimpleNamespace(
        task_svc=types.SimpleNamespace(get=lambda _i: task),
        conductor_svc=object(), memory_svc=None)
    monkeypatch.setattr(wf, "get_project", lambda p: ctx)

    from prism_service.services import gate_adjudicator as ga
    monkeypatch.setattr(ga, "_pending_decline_reason",
                        lambda *a, **k: "needs an explicit owner approval")
    monkeypatch.setattr(wf, "_score_rubric",
                        lambda *a, **k: {"ok": True, "reason": "fine"})
    from prism_service.services import plan_gate_checks as pgc
    monkeypatch.setattr(pgc, "run_all", lambda *a, **k: [
        {"id": "absent_file_claim", "ok": True, "reason": ""}])

    out = wf.workflow_node_status("plan-gate-check", project="prism",
                                  task_id="t1")
    by_id = {n["id"]: n for n in out["nodes"]}
    assert by_id["infer"]["state"] == "not_reached"
    assert "already decided" in by_id["infer"]["reason"]
    assert by_id["rubric"]["state"] == "passed"
