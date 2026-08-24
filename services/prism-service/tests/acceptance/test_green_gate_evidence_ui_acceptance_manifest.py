"""Frozen acceptance manifest guard for the green_gate evidence-completeness
feature (task 3baadd19), same pattern as
test_conductor_tile_acceptance_manifest.py -- see that file's own docstring
for the general rationale (a renamed/removed proof test is a manifest
drift and must fail HERE, not silently pass).

This manifest also carries a `live_walkthrough` section the conductor-tile
one does not: task 3baadd19 was validated live via agent-bridge (remote
assist) against the owner's own tab, not just via source-pinned unit
tests. `live_walkthrough` codifies those exact steps (owner, 2026-08-24:
"make sure you codify how to drive the application as a user to validate
the feature you just checked on") so a future regression suspicion has a
concrete, re-runnable script instead of needing to be re-derived from
scratch every time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent  # .../services/prism-service
_MANIFEST = _HERE.parent / "green_gate_evidence_ui.acceptance.json"


def _load() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def _acs() -> list[dict]:
    return _load()["acceptance_criteria"]


def _node_exists(node_id: str) -> bool:
    """A node-id 'tests/x/y.py::test_fn' resolves iff the file exists on disk and
    defines that function (no pytest collection needed -- dependency-free)."""
    assert "::" in node_id, f"malformed node-id: {node_id!r}"
    rel, func = node_id.split("::", 1)
    path = _SERVICE_ROOT / rel
    if not path.exists():
        return False
    return f"def {func}(" in path.read_text(encoding="utf-8")


def test_manifest_is_present_and_well_formed():
    assert _MANIFEST.exists(), f"missing acceptance manifest: {_MANIFEST}"
    doc = _load()
    assert doc.get("contract") == "green-gate-evidence-ui"
    assert isinstance(_acs(), list) and _acs(), "manifest lists no ACs"


def test_ac_ids_are_unique():
    ids = [ac["id"] for ac in _acs()]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate AC ids in manifest: {dupes}"


@pytest.mark.parametrize("ac", _acs(), ids=lambda a: a["id"])
def test_ac_status_is_valid(ac):
    status = ac["status"]
    assert status == "delivered" or status == "retired" or status.startswith("moved:"), (
        f"AC {ac['id']} has invalid status {status!r}"
    )
    assert ac.get("statement"), f"AC {ac['id']} has no human statement"


@pytest.mark.parametrize("ac", _acs(), ids=lambda a: a["id"])
def test_delivered_ac_has_a_real_proof_node(ac):
    """A 'delivered' AC must cite a test node-id that EXISTS -- this is the
    freeze: a later rename/deletion of the proof is a manifest drift and
    fails here."""
    if ac["status"] != "delivered":
        return
    proof = ac.get("proof_node_id")
    assert proof, f"delivered AC {ac['id']} must name a proof_node_id"
    assert _node_exists(proof), (
        f"delivered AC {ac['id']} cites a MISSING proof node-id {proof!r} -- "
        "manifest/test drift (freeze violated)"
    )


@pytest.mark.parametrize("ac", _acs(), ids=lambda a: a["id"])
def test_retired_or_moved_ac_carries_no_proof_but_a_real_guard(ac):
    if ac["status"] == "delivered":
        return
    assert ac.get("proof_node_id") is None, (
        f"{ac['status']} AC {ac['id']} must not carry a proof_node_id (do not "
        "fake-satisfy a superseded/moved AC)"
    )
    guard = ac.get("guard_node_id")
    assert guard, f"{ac['status']} AC {ac['id']} must name a guard_node_id"
    assert _node_exists(guard), (
        f"AC {ac['id']} cites a MISSING guard node-id {guard!r} -- manifest drift"
    )


# ---------------------------------------------------------------------------
# live_walkthrough: this manifest's own addition over conductor_tile's shape.
# Not a proof of correctness (no browser runs here) -- a structural freeze so
# the SCRIPT itself can't silently rot into vague, unrunnable prose.
# ---------------------------------------------------------------------------


def test_live_walkthrough_is_present_and_ordered():
    doc = _load()
    walk = doc.get("live_walkthrough")
    assert walk and walk.get("steps"), (
        "manifest must carry a live_walkthrough.steps list -- this feature "
        "was validated live via agent-bridge, not just source-pinned tests, "
        "and that procedure must be codified, not left in chat scrollback")
    assert walk.get("how_to_run"), (
        "live_walkthrough must say HOW to run it (which mechanism drives "
        "the browser, what to confirm before starting)")
    steps = walk["steps"]
    numbers = [s["step"] for s in steps]
    assert numbers == sorted(numbers) == list(range(1, len(steps) + 1)), (
        f"live_walkthrough steps must be numbered 1..N in order, got {numbers}")


@pytest.mark.parametrize(
    "step", _load()["live_walkthrough"]["steps"], ids=lambda s: f"step-{s['step']}",
)
def test_live_walkthrough_step_names_a_concrete_action_and_expectation(step):
    """Every step must name a concrete, executable ACTION (a URL, a click
    target, a curl command -- something a person or an agent-bridge command
    literally does) and a concrete, checkable EXPECTATION (what should be
    observed) -- never a vague "verify it looks right"."""
    action = step.get("action", "")
    expect = step.get("expect", "")
    assert len(action) > 15, f"step {step['step']}: action too vague: {action!r}"
    assert len(expect) > 15, f"step {step['step']}: expectation too vague: {expect!r}"
    concrete_markers = ("navigate", "click", "read", "curl", "GET ", "POST ")
    assert any(marker in action for marker in concrete_markers), (
        f"step {step['step']}: action names no concrete browser/API "
        f"operation: {action!r}")
