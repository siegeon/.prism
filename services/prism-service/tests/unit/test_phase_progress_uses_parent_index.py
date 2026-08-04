"""RED - phase_progress's child-count computation must be index-supported
(task 93d6c6f3, AC-5).

TODAY: ConductorService.phase_progress (conductor_service.py:4313-4358)
counts children by calling self._task_svc.list() with NO parent_id filter
(the whole tasks table) and filtering `parent_id == task_id` in PYTHON at
:4345-4346. Confirmed by tracing every SQL statement SQLite executes
during a real phase_progress() call (sqlite3.Connection.set_trace_callback,
prototyped directly against this worktree's task_service.py /
conductor_service.py): zero of the ~14 statements issued carry a
`parent_id = ?` WHERE clause, and the one unfiltered `tasks` SELECT that
IS issued plans as `EXPLAIN QUERY PLAN` -> `SCAN tasks` - even though
`idx_tasks_parent` (task_service.py:95) already exists and
`TaskService.list(parent_id=...)` (task_service.py:583,611-613) already
plans onto it as `SEARCH tasks USING INDEX idx_tasks_parent` (verified the
exact plan text this session). So "an index exists somewhere" is not the
same as "this code path uses it" - the index trap AC-5 and this task's
likely_misfire both name explicitly.

Behavioural, not source-scanning: drives a REAL TaskService against a
temp on-disk sqlite db (mirrors tests/integration/test_phase_progress_seam.py's
_services() helper), traces every statement SQLite executes via SQLite's
own trace hook during phase_progress(), and asserts a parent_id-scoped
statement runs and plans onto the index.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False, task_svc=task_svc,
    )
    return task_svc, cond


def _managed_with_children(task_svc, cond, n_children=3):
    parent = task_svc.create(title="parent-with-children")
    cond.advance_task(parent.id)  # '' -> step 0, workflow_step non-empty
    for i in range(n_children):
        task_svc.create(title=f"child-{i}", parent_id=parent.id)
    return task_svc.get(parent.id)


def _trace_sql(task_svc, fn):
    """Run fn() while recording every SQL statement SQLite executes on
    task_svc's connection - SQLite's own trace hook, not a service mock,
    so this is a claim about the REAL statements the real engine issues."""
    captured: list[str] = []
    task_svc._db.set_trace_callback(lambda stmt: captured.append(stmt))
    try:
        fn()
    finally:
        task_svc._db.set_trace_callback(None)
    return captured


def _parent_filtered_task_selects(statements: list[str]) -> list[str]:
    """SELECTs against `tasks` whose WHERE clause scopes on parent_id - the
    shape a parent_id-scoped child-count query must have."""
    out = []
    for s in statements:
        u = s.upper()
        if "SELECT" in u and " FROM TASKS" in u and "PARENT_ID" in u:
            out.append(s)
    return out


def test_phase_progress_child_count_issues_a_parent_id_scoped_query(tmp_path):
    """RED today: phase_progress's child-count loop
    (conductor_service.py:4345, `for t in self._task_svc.list(): if
    parent_id==task_id`) never issues a parent_id-filtered SQL statement -
    it loads the WHOLE tasks table and filters in Python. Confirmed by
    direct trace: 0 of the statements a real phase_progress() call issues
    carry a parent_id WHERE clause."""
    task_svc, cond = _services(tmp_path)
    parent = _managed_with_children(task_svc, cond, n_children=3)

    statements = _trace_sql(task_svc, lambda: cond.phase_progress(parent.id))

    filtered = _parent_filtered_task_selects(statements)
    assert filtered, (
        "phase_progress(task_id) must count children via a parent_id-scoped "
        "SQL statement (e.g. self._task_svc.list(parent_id=task_id)), not by "
        "loading every task and filtering in Python. Captured statements: "
        f"{statements}"
    )


def test_the_parent_id_scoped_query_plans_onto_the_index_not_a_scan(tmp_path):
    """The second half of AC-5: 'not merely an index exists somewhere' -
    the STATEMENT phase_progress issues (once fixed) must itself plan onto
    idx_tasks_parent (SEARCH), never fall back to a full SCAN tasks. This
    guards the MECHANISM, not just its presence: EXPLAIN QUERY PLAN on the
    exact statement captured from a real phase_progress() call."""
    task_svc, cond = _services(tmp_path)
    parent = _managed_with_children(task_svc, cond, n_children=3)

    statements = _trace_sql(task_svc, lambda: cond.phase_progress(parent.id))
    filtered = _parent_filtered_task_selects(statements)
    assert filtered, "no parent_id-scoped statement was issued (see the companion test)"

    for stmt in filtered:
        plan_rows = task_svc._db.execute("EXPLAIN QUERY PLAN " + stmt).fetchall()
        details = " | ".join(str(row[3]) for row in plan_rows)
        assert "idx_tasks_parent" in details, (
            f"parent_id-scoped query does not use idx_tasks_parent: {details}\n"
            f"statement: {stmt}"
        )
        assert "SCAN tasks" not in details, (
            "parent_id-scoped query still falls back to a full table scan: "
            f"{details}\nstatement: {stmt}"
        )
