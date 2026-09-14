"""A behaviour step that IS a registered multiplier block renders as a
block-styled sub-node inside that behaviour's own canvas, instead of the
block only ever showing up in the separate worker_seat_blocks group
(owner 2026-09-13/14, task b490fabc's lineage: "we should have
multiplier steps before the red that are pydantic to help speed up the
inference"). write-failing-tests-loop.json's targets/pack/compose steps
already dispatch to the exact routes red.targets_from_acs/red.context_
pack/red.prompt_compose are registered under; plan-gate-check.json's
"infer" step reaches certainty.derive_oracle only from inside a Python
branch (design_packet.adjudicate_root_plan_gate), so that one is named
by declared override instead of route-matching.
"""
from __future__ import annotations

import pytest

from prism_service.api import workflows as wf

# Importing these registers every block this repo has declared.
from prism_service.blocks import list_blocks  # noqa: F401
from prism_service.services import deploy_worker  # noqa: F401
from prism_service.services import design_packet  # noqa: F401
from prism_service.services import gate_adjudicator  # noqa: F401
from prism_service.services import resume_actuator  # noqa: F401
from prism_service.services import task_workspace  # noqa: F401
from prism_service.blocks import red_blocks  # noqa: F401


@pytest.fixture
def repo_root_on_disk(monkeypatch, tmp_path):
    from prism_service.services import claude_transcripts as ct

    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(ct, "_project_source_path", lambda project: str(root))
    return root


def _write_failing_tests_loop_engine(path, **_kw):
    if "/behaviors/" in path:
        return {"id": "write-failing-tests-loop", "steps": [
            {"id": "route", "kind": "http-callback",
             "url": "http://x/api/workflows/steps/oracle-route-check"},
            {"id": "targets", "kind": "http-callback",
             "url": "http://x/api/workflows/steps/red-targets-from-acs"},
            {"id": "pack", "kind": "http-callback",
             "url": "http://x/api/workflows/steps/red-context-pack"},
            {"id": "compose", "kind": "http-callback",
             "url": "http://x/api/workflows/steps/red-prompt-compose"},
            {"id": "loop", "kind": "http-callback",
             "url": "http://x/api/workflows/steps/reason-loop"},
        ]}
    return {"fsms": [{"fsmId": "pipeline",
                      "behaviorIds": ["write-failing-tests-loop"]}]}


def test_a_step_whose_route_is_a_block_slug_carries_block_id(monkeypatch,
                                                              repo_root_on_disk):
    monkeypatch.setattr(wf, "_workflow_engine_json",
                        _write_failing_tests_loop_engine)

    entries = wf._conductor_behavior_workflows("prism")
    by_id = {s["id"]: s for s in entries[0]["steps"]}

    assert by_id["targets"]["block_id"] == "red.targets_from_acs"
    assert by_id["targets"]["block_kind"] == "deterministic"
    assert by_id["pack"]["block_id"] == "red.context_pack"
    assert by_id["compose"]["block_id"] == "red.prompt_compose"

    # Clicking this sub-node must read IDENTICALLY to clicking the same
    # block's own card in the worker_seat_blocks group view -- purpose/
    # input/action/output mirror the block's title/inputs/description/
    # outputs, not a second, drifting description of the same step.
    from prism_service.blocks import get_block
    block = get_block("red.targets_from_acs")
    assert by_id["targets"]["purpose"] == block.title
    assert by_id["targets"]["input"] == ", ".join(block.inputs)
    assert by_id["targets"]["action"] == block.description
    assert by_id["targets"]["output"] == ", ".join(block.outputs)
    # the agentic "loop" step and the plain http "route" step are not
    # registered blocks -- no block_id at all, and the step order the
    # behaviour declared is unchanged (three typed blocks ahead of the
    # agentic step, never reordered or duplicated).
    assert by_id["route"]["block_id"] is None
    assert by_id["loop"]["block_id"] is None
    assert [s["id"] for s in entries[0]["steps"]] == [
        "route", "targets", "pack", "compose", "loop"]


def _plan_gate_check_engine(path, **_kw):
    if "/behaviors/" in path:
        return {"id": "plan-gate-check", "steps": [
            {"id": "rubric", "kind": "http-callback",
             "url": "http://x/api/workflows/steps/plan-gate-check"},
            {"id": "infer", "kind": "http-callback",
             "url": "http://x/api/workflows/steps/gate-adjudication"},
        ]}
    return {"fsms": [{"fsmId": "pipeline",
                      "behaviorIds": ["plan-gate-check"]}]}


def test_plan_gate_check_shows_certainty_derive_oracle(monkeypatch,
                                                        repo_root_on_disk):
    monkeypatch.setattr(wf, "_workflow_engine_json", _plan_gate_check_engine)

    entries = wf._conductor_behavior_workflows("prism")
    ids = [s["id"] for s in entries[0]["steps"]]
    assert ids == ["rubric", "infer", "infer__certainty.derive_oracle"], ids

    synthetic = entries[0]["steps"][2]
    assert synthetic["block_id"] == "certainty.derive_oracle"
    assert synthetic["block_kind"] == "deterministic"
    # never dispatched on its own -- the live plan-gate-check.json is
    # untouched, this node is display-only
    assert synthetic["execution"] == "connected"
    assert synthetic["route"] == "certainty.derive_oracle"


def test_a_behaviour_with_no_matching_block_carries_none(monkeypatch,
                                                          repo_root_on_disk):
    def _engine(path, **_kw):
        if "/behaviors/" in path:
            return {"id": "review-previous-notes-loop", "steps": [
                {"id": "gather", "kind": "http-callback",
                 "url": "http://x/api/workflows/steps/premise-gather"}]}
        return {"fsms": [{"fsmId": "pipeline",
                          "behaviorIds": ["review-previous-notes-loop"]}]}

    monkeypatch.setattr(wf, "_workflow_engine_json", _engine)
    entries = wf._conductor_behavior_workflows("prism")
    assert entries[0]["steps"][0]["block_id"] is None
