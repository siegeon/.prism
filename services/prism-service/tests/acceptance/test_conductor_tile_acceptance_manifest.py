"""Frozen acceptance manifest guard for the conductor showcase tile
(inverted-flow #4 — 'freeze acceptance-test node-ids').

The owner decided the branch's showcase ConductorPage SUPERSEDES the PR-#212
ring+timeline tile. Earlier commits (89194d6, d512190) laundered the acceptance
tests by re-pointing them at whatever the branch happened to render (a comment,
a source-scan of an unrendered feature, a different component). This manifest
FREEZES the honest contract:

  * every 'delivered' AC must name a REAL test node-id that exists on disk —
    a renamed/removed proof (drift between manifest and tests) fails HERE;
  * a 'delivered' AC may not lack a proof;
  * 'retired'/'moved:<task>' ACs carry NO proof but must name a guard test that
    pins the retirement premise, and that guard must exist too.

This is the tripwire: you cannot silently re-launder a test without either the
proof going missing (this fails) or updating the manifest to say so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent  # .../services/prism-service
_MANIFEST = _HERE.parent / "conductor_tile.acceptance.json"


def _load() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def _acs() -> list[dict]:
    return _load()["acceptance_criteria"]


def _node_exists(node_id: str) -> bool:
    """A node-id 'tests/x/y.py::test_fn' resolves iff the file exists on disk and
    defines that function (no pytest collection needed — dependency-free)."""
    assert "::" in node_id, f"malformed node-id: {node_id!r}"
    rel, func = node_id.split("::", 1)
    path = _SERVICE_ROOT / rel
    if not path.exists():
        return False
    return f"def {func}(" in path.read_text(encoding="utf-8")


def test_manifest_is_present_and_well_formed():
    assert _MANIFEST.exists(), f"missing acceptance manifest: {_MANIFEST}"
    doc = _load()
    assert doc.get("contract") == "conductor-tile"
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
    """A 'delivered' AC must cite a test node-id that EXISTS — this is the freeze:
    a later rename/deletion of the proof is a manifest drift and fails here."""
    if ac["status"] != "delivered":
        return
    proof = ac.get("proof_node_id")
    assert proof, f"delivered AC {ac['id']} must name a proof_node_id"
    assert _node_exists(proof), (
        f"delivered AC {ac['id']} cites a MISSING proof node-id {proof!r} — "
        "manifest/test drift (freeze violated)"
    )


@pytest.mark.parametrize("ac", _acs(), ids=lambda a: a["id"])
def test_retired_or_moved_ac_carries_no_proof_but_a_real_guard(ac):
    """A retired/moved AC must NOT fake a proof, and must name a guard test that
    pins the retirement premise (e.g. 'the tile renders no TokenTurns')."""
    if ac["status"] == "delivered":
        return
    assert ac.get("proof_node_id") is None, (
        f"{ac['status']} AC {ac['id']} must not carry a proof_node_id (do not "
        "fake-satisfy a superseded/moved AC)"
    )
    guard = ac.get("guard_node_id")
    assert guard, f"{ac['status']} AC {ac['id']} must name a guard_node_id"
    assert _node_exists(guard), (
        f"AC {ac['id']} cites a MISSING guard node-id {guard!r} — manifest drift"
    )
