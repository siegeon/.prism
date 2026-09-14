"""The planner is told the pinned suite's colour at base (round 2, tasks
6bc3e6c2 and 83dcd479, 2026-09-14).

plan_gate's already_green_ac tooth refuses a plan in which no AC is shown
RED at base and says "measure it there" -- to a tool-less narrow model that
cannot run anything. Every rewind re-drafted guards, spent the budget and
parked for a person. A codified `colour` step now measures once and hands
the planner the fact and the declaration the tooth accepts.

Pins:
- AC-1  plan_base_colour_block frames red / green / unmeasured, and is ""
        with no pinned targets; the red frame carries the `RED at base:`
        words the tooth's _RED_RE accepts.
- AC-2  the step never raises and reports colour=unmeasured on failure.
- AC-3  verify-plan-loop.json runs `colour` between `recall` and `loop`,
        and the loop prompt interpolates ${baseColourBlock}.
- AC-4  the runner maps the route, base variables carry baseColourBlock,
        and plan.base_colour is a registered block.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from prism_service.api import workflows as wf
from prism_service.services import plan_gate_checks as pgc
from prism_service.services import task_runner

_NODE = (Path(__file__).resolve().parents[4]
         / ".prism" / "behaviors" / "conductor" / "verify-plan-loop.json")
_T = ["services/prism-service/tests/unit/test_web_react_pin.py"]


# AC-1 ------------------------------------------------------------------
def test_the_red_frame_carries_the_declaration_the_tooth_accepts():
    block = wf.plan_base_colour_block(1, "0123456789ab", _T)
    assert block.startswith("MEASURED at base 01234567: the pinned suite FAILS (rc=1)")
    assert pgc._RED_RE.search(block), "the frame must teach the tooth's own words"
    assert _T[0] in block


def test_the_green_and_unmeasured_frames_and_the_empty_case():
    green = wf.plan_base_colour_block(0, "abc", _T)
    assert "already PASSES (rc=0)" in green and "already done" in green
    assert "NEW test" in green
    assert wf.plan_base_colour_block(None, "abc", _T).startswith("NOT MEASURED")
    assert wf.plan_base_colour_block(1, "abc", []) == ""


# AC-2 ------------------------------------------------------------------
def test_the_step_measures_with_the_gates_runner(monkeypatch):
    task = SimpleNamespace(id="83dcd479", verify=_T)
    ctx = SimpleNamespace(task_svc=SimpleNamespace(get=lambda tid: task))
    monkeypatch.setattr(wf, "get_project", lambda project: ctx)
    monkeypatch.setattr(pgc, "repo_root_for", lambda t, p: Path("/tmp"))
    monkeypatch.setattr(pgc, "base_ref_for", lambda t, r: "deadbeefcafe")
    monkeypatch.setattr(pgc, "measurement_enabled", lambda: True)
    monkeypatch.setattr(pgc, "_run_at_rev", lambda root, rev, targets, **kw: 1)
    resp = wf.workflow_step_plan_base_colour(
        wf.PlanBaseColourRequest(task_id="83dcd479"), project="prism")
    assert resp.colour == "red" and resp.rc == 1 and resp.base == "deadbeefcafe"
    assert "RED at base" in resp.base_colour_block


def test_the_step_never_raises(monkeypatch):
    def _boom(project):
        raise RuntimeError("no project")
    monkeypatch.setattr(wf, "get_project", _boom)
    resp = wf.workflow_step_plan_base_colour(
        wf.PlanBaseColourRequest(task_id="x"), project="prism")
    assert resp.colour == "" and resp.base_colour_block == ""


# AC-3 ------------------------------------------------------------------
def test_the_node_measures_between_recall_and_loop():
    node = json.loads(_NODE.read_text(encoding="utf-8"))
    ids = [s["id"] for s in node["steps"]]
    assert ids.index("recall") < ids.index("colour") < ids.index("loop")
    colour = next(s for s in node["steps"] if s["id"] == "colour")
    assert "/api/workflows/steps/plan-base-colour" in colour["url"]
    loop = next(s for s in node["steps"] if s["id"] == "loop")
    assert "${baseColourBlock}" in loop["body"]
    assert node["version"] >= 8


# AC-4 ------------------------------------------------------------------
def test_the_runner_maps_the_route_and_the_block_is_registered():
    assert "plan-base-colour" in task_runner._step_handlers()
    task = SimpleNamespace(title="t", description="d", gate_reason="")
    assert task_runner._build_step_variables(task, "id", "prism")["baseColourBlock"] == ""
    from prism_service import blocks as _blocks
    import prism_service.blocks.plan_blocks  # noqa: F401
    assert _blocks.get_block("plan.base_colour") is not None
