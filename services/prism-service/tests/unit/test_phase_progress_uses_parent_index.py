"""RED scaffold — phase_progress reads children through the parent index, and
the corpus-median cache refreshes on a FOREIGN advance_task write
(task dee931c6, slice B/C of 93d6c6f3).

Two performance levers already landed at base commit 5a7f0e396f35:
conductor_service.py:5654 reads `self._task_svc.list(parent_id=task_id)`, and
_median_step_s / _per_step_typical both call `TaskService.advance_rows_all()`
(task_service.py:1410-1433), one SELECT instead of a per-task history() fanout.
NOTHING pins either one, so the next refactor of those three methods can
re-introduce `SCAN tasks` or the N+1 in silence. AC-1 and AC-2 below are that
guard: they trace the REAL SQL a real phase_progress() call issues, never the
source text of conductor_service.py.

AC-7 is the criterion that is RED at the base commit, measured not predicted.
`_advance_rows_cache` is INSTANCE state (task_service.py:366) cleared only
inside the TaskService that writes the advance_task row (:542, :572), and
advance_rows_all returns the cached dict whenever it is not None (:1421-1422).
So an advance_task row written by a DIFFERENT process against the same
tasks.db never reaches this process, and every viewer on it reads a stale
median and a stale ETA for the life of the process. The task's own stop_if
asks for a cache keyed on the newest history row, never a bare TTL, which is
why test_a_foreign_advance_task_write_reaches_a_warm_cache does no sleeping:
freshness must follow the write, not the clock.

test_the_corpus_cache_still_serves_a_repeat_call_without_rescanning is GREEN at
base and stays green: deleting the cache would fix AC-7 and give back the
per-render full scan the cache exists to prevent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# A per-task history read: `... task_history ... task_id = <one value>`. The
# fanout AC-2 forbids is this statement issued once PER TASK IN THE STORE.
_PER_TASK_HISTORY = re.compile(
    r"task_history[\s\S]*?task_id\s*=", re.IGNORECASE)
# The project-wide corpus scan advance_rows_all issues on a cold cache.
_CORPUS_SCAN = re.compile(
    r"task_history[\s\S]*?action\s*=\s*[?']?advance_task", re.IGNORECASE)
# The child-count lookup: a SELECT over `tasks` filtered on parent_id. The
# sqlite3 trace callback hands back the EXPANDED statement (parameters already
# substituted), so the value side is a literal here, not a '?'.
_PARENT_SCOPED = re.compile(
    r"from\s+tasks[\s\S]*?parent_id\s*=", re.IGNORECASE)

_SIBLINGS = 12


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False, task_svc=task_svc)
    return task_svc, cond


def _seeded_parent(task_svc, cond):
    """A conductor-managed parent with _SIBLINGS children, plus _SIBLINGS
    unrelated tasks, so a full-table load and a parent-scoped read return
    visibly different row counts."""
    parent = task_svc.create(title="parent under measurement")
    cond.advance_task(parent.id)
    for i in range(_SIBLINGS):
        task_svc.create(title=f"child {i}", parent_id=parent.id)
        task_svc.create(title=f"unrelated {i}")
    return parent


def _traced(task_svc, fn):
    """Run fn() with a sqlite3 trace on the connection TaskService uses, and
    return (result, [statements issued])."""
    seen: list[str] = []
    conn = task_svc._db
    conn.set_trace_callback(seen.append)
    try:
        result = fn()
    finally:
        conn.set_trace_callback(None)
    return result, seen


# ----------------------------------------------------------------------
# AC-1 — the child-count lookup is index-supported
# ----------------------------------------------------------------------

def test_phase_progress_issues_a_parent_id_scoped_statement(tmp_path):
    task_svc, cond = _services(tmp_path)
    parent = _seeded_parent(task_svc, cond)

    pp, statements = _traced(task_svc, lambda: cond.phase_progress(parent.id))

    assert pp["children_total"] == _SIBLINGS, (
        "the seed is wrong if the parent does not see its own children")
    scoped = [s for s in statements if _PARENT_SCOPED.search(s)]
    assert scoped, (
        "phase_progress must read children with a parent_id-scoped SELECT "
        "over `tasks`; it issued none. Statements over `tasks`: "
        + repr([s for s in statements if "tasks" in s.lower()][:6]))


def test_the_parent_id_statement_plans_onto_idx_tasks_parent(tmp_path):
    task_svc, cond = _services(tmp_path)
    parent = _seeded_parent(task_svc, cond)

    _, statements = _traced(task_svc, lambda: cond.phase_progress(parent.id))
    scoped = [s for s in statements if _PARENT_SCOPED.search(s)]
    assert scoped, "no parent_id-scoped statement was issued"

    for stmt in scoped:
        plan = " | ".join(
            str(r[3]) for r in
            task_svc._db.execute("EXPLAIN QUERY PLAN " + stmt).fetchall())
        assert "idx_tasks_parent" in plan, (
            f"the child-count read must SEARCH USING INDEX idx_tasks_parent; "
            f"sqlite planned it as: {plan}")
        assert "SCAN tasks" not in plan, (
            f"a full table SCAN of `tasks` is the defect this slice removes; "
            f"sqlite planned it as: {plan}")


# ----------------------------------------------------------------------
# AC-2 — the corpus medians issue no per-task history fanout
# ----------------------------------------------------------------------

def test_phase_progress_issues_no_per_task_history_fanout(tmp_path):
    task_svc, cond = _services(tmp_path)
    parent = _seeded_parent(task_svc, cond)
    # 25 rows live in `tasks`; a fanout would issue one history read per row.
    _, statements = _traced(task_svc, lambda: cond.phase_progress(parent.id))

    per_task = [s for s in statements if _PER_TASK_HISTORY.search(s)]
    assert len(per_task) <= 3, (
        f"the project-wide step medians must not read history PER TASK: "
        f"{len(per_task)} per-task task_history statements for a store of "
        f"{2 * _SIBLINGS + 1} tasks. First few: {per_task[:4]}")


# ----------------------------------------------------------------------
# AC-7 — the corpus-median cache refreshes on a FOREIGN advance_task write
# ----------------------------------------------------------------------

def test_a_foreign_advance_task_write_reaches_a_warm_cache(tmp_path):
    from prism_service.services.task_service import TaskService

    reader, cond = _services(tmp_path)
    parent = _seeded_parent(reader, cond)

    warm = reader.advance_rows_all()
    before = len(warm.get(parent.id, []))

    # A SECOND service on the SAME db file — the daemon's other worker, the
    # gate adjudicator, a sibling process. It records a real advance.
    writer = TaskService(str(tmp_path / "tasks.db"))
    writer.record_history(
        parent.id, "advance_task",
        details="write_failing_tests -> implement_tasks", actor="other")
    assert len(writer.advance_rows_all().get(parent.id, [])) == before + 1, (
        "the writing service must see its own row — the seed is wrong")

    after = reader.advance_rows_all()
    assert len(after.get(parent.id, [])) == before + 1, (
        "advance_rows_all served a cached snapshot that predates a foreign "
        "advance_task write, so every median and ETA computed from it is "
        "stale for the life of this process. The cache must key on the "
        "newest history row, never on this instance's own writes alone.")


def test_the_corpus_cache_still_serves_a_repeat_call_without_rescanning(
        tmp_path):
    """Deleting the cache would fix the test above and give back the
    per-render full scan the cache exists to prevent. With NO new history
    row, a repeat call must not re-issue the corpus scan."""
    task_svc, cond = _services(tmp_path)
    _seeded_parent(task_svc, cond)

    task_svc.advance_rows_all()  # warm
    _, statements = _traced(task_svc, task_svc.advance_rows_all)

    scans = [s for s in statements if _CORPUS_SCAN.search(s)]
    assert not scans, (
        f"no advance_task row was written between the two calls, so the "
        f"second one must be served from cache; it re-scanned: {scans}")
