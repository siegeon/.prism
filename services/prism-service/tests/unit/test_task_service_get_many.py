"""TaskService.get_many batches by-id reads (route-timing pass, owner
brief 2026-09-13, final item): GET /api/work/graph's node-building loop
called task_svc.get(id) once PER NODE -- 122 separate SQLite round trips
on the live instance, the dominant remaining cost in the route's `edges`
phase once queue_depth/phase_progress/activity_for were already batched.
One `WHERE id IN (...)` query for the whole set instead."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _svc(tmp_path):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / "tasks.db"))


def test_get_many_returns_every_matching_task(tmp_path):
    svc = _svc(tmp_path)
    a = svc.create(title="a")
    b = svc.create(title="b")
    c = svc.create(title="c")

    got = svc.get_many([a.id, b.id, c.id])
    assert set(got.keys()) == {a.id, b.id, c.id}
    assert got[a.id].title == "a"
    assert got[b.id].title == "b"
    assert got[c.id].title == "c"


def test_get_many_matches_get_field_for_field(tmp_path):
    svc = _svc(tmp_path)
    t = svc.create(title="fields must match")
    svc.update(t.id, status="in_progress", workflow_step="implement_tasks",
               gate_state="pending")

    single = svc.get(t.id)
    many = svc.get_many([t.id])[t.id]
    assert many.id == single.id
    assert many.title == single.title
    assert many.status == single.status
    assert many.workflow_step == single.workflow_step
    assert many.gate_state == single.gate_state
    assert many.updated_at == single.updated_at


def test_get_many_omits_ids_that_do_not_exist(tmp_path):
    svc = _svc(tmp_path)
    t = svc.create(title="real")

    got = svc.get_many([t.id, "no-such-id"])
    assert set(got.keys()) == {t.id}


def test_get_many_empty_list_returns_empty_dict_no_query(tmp_path):
    svc = _svc(tmp_path)
    assert svc.get_many([]) == {}


def test_get_many_dedupes_repeated_ids(tmp_path):
    svc = _svc(tmp_path)
    t = svc.create(title="dup")
    got = svc.get_many([t.id, t.id, t.id])
    assert set(got.keys()) == {t.id}


def test_get_many_issues_one_query_not_one_per_id(tmp_path):
    """Traces the real SQL via sqlite3's own trace callback, same
    convention as test_phase_progress_uses_parent_index.py /
    test_conductor_state_and_work_graph_query_cost.py: an id-scoped
    SELECT must appear at most once for the whole batch, not once per
    id."""
    svc = _svc(tmp_path)
    ids = [svc.create(title=f"t{i}").id for i in range(5)]

    statements: list[str] = []
    conn = svc._db
    conn.set_trace_callback(statements.append)
    try:
        svc.get_many(ids)
    finally:
        conn.set_trace_callback(None)

    id_scoped = [s for s in statements if "FROM tasks WHERE id IN" in s]
    assert len(id_scoped) == 1, (
        f"expected exactly one batched id-scoped SELECT for 5 ids; "
        f"got {len(id_scoped)}: {statements!r}")


def test_get_many_columns_projection_still_defaults_missing_fields(tmp_path):
    svc = _svc(tmp_path)
    t = svc.create(title="narrow")
    svc.update(t.id, status="in_progress")

    got = svc.get_many([t.id], columns=["title", "status"])[t.id]
    assert got.title == "narrow"
    assert got.status == "in_progress"
    # A column left out of the narrow SELECT falls back to the dataclass
    # default, never KeyError -- same contract as list(columns=...).
    assert got.description == ""
