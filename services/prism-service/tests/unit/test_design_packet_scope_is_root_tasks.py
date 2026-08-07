"""Design-packet sign-off is asked of the owner for ROOT tasks only.

Owner 2026-08-06: "the children sub-tasks can not be blocked, as only you
can do the work, and you need to validate sub tasks, you only involve me at
parent task level".

Today every task's plan_gate waits for an explicit owner approval of its
design packet. For a root task that is exactly right and is unchanged here.
For a child it is a dead end: the driver owns the slice, so the gate waits
for someone who never intended to look at it. Measured on three children of
epic 0784729f, all identical -- readiness returns adapter "design-packet",
"no approval is on file yet" -- while the epic's progress, counted in
children DONE, sat at 7/13 for an entire session with a dozen commits of
finished code behind it.

Scoped to root tasks. The owner keeps both stops on everything they watch.

The guard that must NOT loosen, pinned below: `record_approval` still
accepts only `owner_explicit`, so a driver can never record a sign-off in
the owner's name, and a root task still re-parks when its packet is edited
after approval.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


@dataclass
class _Task:
    """Only the fields the packet hash and the scoping actually read."""

    id: str = "t1"
    parent_id: str = ""
    title: str = "a task"
    plan_doc: str = "## Summary\nsomething"
    plan_diagram: str = "flowchart LR\n  A --> B"
    oracle: str = "an observable signal"
    likely_misfire: str = "a way it could pass but be wrong"
    tags: tuple = ()
    proof_type: str = "test"


@pytest.fixture
def dp(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    from prism_service.services import design_packet as mod

    return mod


def test_a_child_slice_is_the_drivers_to_validate(dp):
    """A child carries no owner sign-off requirement. Nothing is on disk."""
    child = _Task(id="child-1", parent_id="epic-1")
    status = dp.approval_status("default", child.id, child)
    assert status["approved"] is True, status
    assert status.get("stale") is False


def test_a_root_task_still_waits_for_the_owner(dp):
    """Unchanged for everything the owner actually watches."""
    root = _Task(id="root-1", parent_id="")
    status = dp.approval_status("default", root.id, root)
    assert status["approved"] is False
    assert "owner approval" in status["reason"]


def test_a_root_task_re_parks_when_its_packet_changes(dp):
    """The scoping must not short-circuit the read-time hash recompute that
    catches a design rewritten after sign-off."""
    root = _Task(id="root-2", parent_id="")
    dp.record_approval("default", root.id, root, approver="owner",
                       method="owner_explicit")
    assert dp.approval_status("default", root.id, root)["approved"] is True

    edited = _Task(id="root-2", parent_id="",
                   plan_doc="## Summary\nrewritten after sign-off")
    after = dp.approval_status("default", edited.id, edited)
    assert after["approved"] is False
    assert after["stale"] is True


def test_only_the_owner_can_record_a_sign_off(dp):
    """The guard that must never loosen: no caller may record an approval in
    the owner's name. Scoping changes WHO IS ASKED, never who may answer."""
    assert dp._ALLOWED_METHODS == {"owner_explicit"}
    root = _Task(id="root-3", parent_id="")
    for method in ("machine", "browser", "render", "conductor-adjudicator"):
        with pytest.raises(ValueError):
            dp.record_approval("default", root.id, root, approver="agent",
                               method=method)
