"""Hierarchical tasks — parent_id round-trip + roots/children semantics.

Pins v6.2.20: parent_id is a first-class Task field, additive on tasks.db,
distinct from `dependencies`. The /tasks board shows only roots; a parent's
children are the tasks naming it via parent_id.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _mk_service(tmp_path: Path):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / "tasks.db"))


def test_parent_id_defaults_to_root(tmp_path):
    """A task created without parent_id is a root ('')"""
    svc = _mk_service(tmp_path)
    t = svc.create(title="epic")
    assert t.parent_id == ""
    assert svc.get(t.id).parent_id == ""


def test_parent_id_round_trips_on_create(tmp_path):
    """parent_id passed to create persists and reads back."""
    svc = _mk_service(tmp_path)
    parent = svc.create(title="epic")
    child = svc.create(title="child", parent_id=parent.id)
    assert child.parent_id == parent.id
    assert svc.get(child.id).parent_id == parent.id


def test_parent_id_settable_via_update(tmp_path):
    """A root task can be re-parented (the backfill path)."""
    svc = _mk_service(tmp_path)
    parent = svc.create(title="epic")
    child = svc.create(title="orphan")
    assert child.parent_id == ""
    updated = svc.update(child.id, parent_id=parent.id)
    assert updated.parent_id == parent.id
    assert svc.get(child.id).parent_id == parent.id


def test_roots_and_children_partition(tmp_path):
    """The board's 'roots only' view = tasks with empty parent_id; the
    rest are reachable as children grouped by parent_id."""
    svc = _mk_service(tmp_path)
    epic = svc.create(title="epic")
    a = svc.create(title="a", parent_id=epic.id)
    b = svc.create(title="b", parent_id=epic.id)
    solo = svc.create(title="solo")

    everything = svc.list()
    roots = [t for t in everything if not t.parent_id]
    children = [t for t in everything if t.parent_id == epic.id]

    assert {t.id for t in roots} == {epic.id, solo.id}
    assert {t.id for t in children} == {a.id, b.id}


def test_parent_id_independent_of_dependencies(tmp_path):
    """parent_id (hierarchy) and dependencies (blocking) are separate
    fields and don't bleed into each other."""
    svc = _mk_service(tmp_path)
    epic = svc.create(title="epic")
    child = svc.create(title="child", parent_id=epic.id, dependencies=["x"])
    got = svc.get(child.id)
    assert got.parent_id == epic.id
    assert got.dependencies == ["x"]
