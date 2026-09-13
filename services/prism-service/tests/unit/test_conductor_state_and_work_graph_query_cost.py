"""GET /api/conductor/state and GET /api/work/graph must not re-derive the
SAME project-wide numbers once per managed task (tick-cost pass, external
fixer, owner brief 2026-09-13 -- no PRISM ticket, sibling of the deploy_worker
fetch-cache and standing-worker fixes already landed this round).

Measured live on the real daemon at 7.13.338, 16s after a cold start:
GET /api/conductor/state 1.53s, GET /api/work/graph 2.72s, while
/api/system/activity answered in 3ms. Both routes poll every 1-2s from the
Workflows/Live pages. In-process profiling against a copy of the real
project's databases (cProfile, no code changes) found the actual cost:
conductor_service.py's phase_progress() calls _median_step_s() and
_per_step_typical() -- both O(every advance_task row in the project) with
regex + datetime.fromisoformat parsing -- ONCE PER MANAGED TASK, so a
58-task render redid the identical project-wide computation 58 times.
Separately, task_service.py's history(task_id) had no cache of its own:
_in_step_s / _own_transition_run_start / _task_window_start each called it
independently for the SAME task_id inside one render (~4x per task), and
list(parent_id=X) was independently reissued by _children / _queue_depth /
_task_motion_s for the same reason. managed_tasks() / step_buckets() /
_board_tasks() also each ran their OWN unfiltered `SELECT * FROM tasks` per
request -- three full-table loads for one page render.

These tests trace the REAL SQL a real state()/work_graph() call issues
(never source text) across a store with many managed tasks, so a future
change that reintroduces any of the four fanouts goes red here rather than
only showing up as a live latency regression. AC-5/AC-6 assert the
transcript/git-shaped fixed costs already in this file (bounded transcript
I/O, the dirty-judge git check) stay bounded too: zero subprocess calls when
no gate is pending.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_N_TASKS = 40

# A per-task history read: `... task_history ... task_id = <value>` (the
# sqlite3 trace callback hands back the EXPANDED statement, value already
# substituted for the placeholder).
_PER_TASK_HISTORY = re.compile(
    r"task_history[\s\S]*?task_id\s*=", re.IGNORECASE)
# The project-wide corpus scan advance_rows_all issues on a cold cache.
_CORPUS_SCAN = re.compile(
    r"task_history[\s\S]*?action\s*=\s*[?']?advance_task", re.IGNORECASE)
# A parent_id-scoped SELECT over `tasks` (list(parent_id=X)).
_PARENT_SCOPED = re.compile(
    r"from\s+tasks[\s\S]*?parent_id\s*=", re.IGNORECASE)
# A bare, unfiltered board load: list() with no WHERE clause at all.
_UNFILTERED_BOARD = re.compile(
    r"^select\s+\*\s+from\s+tasks\s+order\s+by", re.IGNORECASE)


def _seed(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    ids = []
    for i in range(_N_TASKS):
        t = task_svc.create(title=f"managed task {i}")
        task_svc.update(t.id, status="in_progress")
        cond.advance_task(t.id)  # '' -> first WORKFLOW_STEPS entry
        cond.advance_task(t.id)  # a second row so per-step medians have data
        ids.append(t.id)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = cond
    ctx.task_svc = task_svc
    return ctx, task_svc, ids


def _traced(task_svc, fn):
    seen: list[str] = []
    conn = task_svc._db
    conn.set_trace_callback(seen.append)
    try:
        result = fn()
    finally:
        conn.set_trace_callback(None)
    return result, seen


def _state_client(ctx, monkeypatch):
    from prism_service.api import conductor as conductor_api
    monkeypatch.setattr(conductor_api, "get_project", lambda p: ctx)
    conductor_api._state_cache.clear()
    return conductor_api


def _graph_client(ctx, monkeypatch):
    from prism_service.api import work as work_api
    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    return work_api


# ----------------------------------------------------------------------
# AC-1/AC-2 -- no per-task history fanout across a many-task render
# ----------------------------------------------------------------------

def test_conductor_state_history_reads_dont_scale_with_a_second_pass(
        tmp_path, monkeypatch):
    """A render must issue AT MOST ONE task_history-by-id read per distinct
    task -- never several. Regression guard for the pre-fix shape, where
    _in_step_s/_own_transition_run_start/_task_window_start each reissued
    it independently for the same task_id."""
    ctx, task_svc, ids = _seed(tmp_path)
    conductor_api = _state_client(ctx, monkeypatch)

    _, statements = _traced(task_svc, lambda: conductor_api.state(project="p"))

    per_task = [s for s in statements if _PER_TASK_HISTORY.search(s)]
    assert len(per_task) <= _N_TASKS, (
        f"expected at most one task_history-by-id read per task "
        f"({_N_TASKS} tasks), got {len(per_task)} -- history(task_id) is "
        f"being re-fetched for the same task within one render")


def test_work_graph_history_reads_dont_scale_with_a_second_pass(
        tmp_path, monkeypatch):
    ctx, task_svc, ids = _seed(tmp_path)
    work_api = _graph_client(ctx, monkeypatch)

    _, statements = _traced(task_svc, lambda: work_api.work_graph(project="p"))

    per_task = [s for s in statements if _PER_TASK_HISTORY.search(s)]
    assert len(per_task) <= _N_TASKS, (
        f"expected at most one task_history-by-id read per task "
        f"({_N_TASKS} tasks), got {len(per_task)}")


# ----------------------------------------------------------------------
# AC-3 -- the corpus-median computation issues its scan at most once
# ----------------------------------------------------------------------

def test_conductor_state_issues_one_corpus_scan_not_one_per_task(
        tmp_path, monkeypatch):
    ctx, task_svc, ids = _seed(tmp_path)
    conductor_api = _state_client(ctx, monkeypatch)

    _, statements = _traced(task_svc, lambda: conductor_api.state(project="p"))

    scans = [s for s in statements if _CORPUS_SCAN.search(s)]
    assert len(scans) <= 1, (
        f"_median_step_s/_per_step_typical must share ONE corpus scan per "
        f"render (they memoize on advance_rows_all's cached object "
        f"identity); got {len(scans)} for {_N_TASKS} managed tasks")


# ----------------------------------------------------------------------
# AC-4 -- parent_id-scoped child reads and the unfiltered board load are
# each issued at most once per distinct key, not 2-3x per task
# ----------------------------------------------------------------------

def test_conductor_state_parent_scoped_reads_are_not_reissued_per_task(
        tmp_path, monkeypatch):
    ctx, task_svc, ids = _seed(tmp_path)
    conductor_api = _state_client(ctx, monkeypatch)

    _, statements = _traced(task_svc, lambda: conductor_api.state(project="p"))

    scoped = [s for s in statements if _PARENT_SCOPED.search(s)]
    assert len(scoped) <= _N_TASKS, (
        f"list(parent_id=X) must be cached per task within a render "
        f"(_children/_queue_depth/_task_motion_s each called it "
        f"independently pre-fix); got {len(scoped)} for {_N_TASKS} tasks")


def test_conductor_state_loads_the_unfiltered_board_at_most_once(
        tmp_path, monkeypatch):
    """managed_tasks()/step_buckets()/_board_tasks() each ran their OWN
    full `SELECT * FROM tasks` per request pre-fix -- three full scans for
    one page render, regardless of task count."""
    ctx, task_svc, ids = _seed(tmp_path)
    conductor_api = _state_client(ctx, monkeypatch)

    _, statements = _traced(task_svc, lambda: conductor_api.state(project="p"))

    boards = [s for s in statements if _UNFILTERED_BOARD.search(s)]
    assert len(boards) <= 1, (
        f"expected at most one unfiltered board load per /state render; "
        f"got {len(boards)}: {boards}")


def test_a_second_state_call_within_the_ttl_skips_the_heavy_recompute(
        tmp_path, monkeypatch):
    """A repeat /state poll inside the 2.5s TTL must be served from the
    existing managed_tasks()/step_buckets()/board_health payload cache --
    _state_payload's own TTL cache (api/conductor.py:220). The report-
    signal/drive-seat enrichment (_with_report_signal/_with_drive_seat)
    deliberately runs OUTSIDE that cache on every call by design (task
    e9625a4d/1c6d59e9: "so staleness stays live") -- cheap, one
    by-id task_svc.get() + one drive_heartbeat.latest() per row, already
    bounded to O(tasks), not O(tasks) MORE THAN ONCE. So this asserts the
    EXPENSIVE, cache-worthy statements are gone on the second call, not
    that the route issues zero SQL."""
    ctx, task_svc, ids = _seed(tmp_path)
    conductor_api = _state_client(ctx, monkeypatch)

    conductor_api.state(project="p")  # warm the TTL cache
    _, statements = _traced(task_svc, lambda: conductor_api.state(project="p"))

    for label, pattern in (
        ("unfiltered board load", _UNFILTERED_BOARD),
        ("per-task history read", _PER_TASK_HISTORY),
        ("corpus scan", _CORPUS_SCAN),
        ("parent_id-scoped read", _PARENT_SCOPED),
    ):
        hits = [s for s in statements if pattern.search(s)]
        assert not hits, (
            f"a /state call inside the TTL window re-ran a {label} instead "
            f"of serving the cached managed_tasks payload: {hits[:3]}")


# ----------------------------------------------------------------------
# AC-5/AC-6 -- no subprocess call on the request path when nothing is
# pending a gate decision
# ----------------------------------------------------------------------

def test_conductor_state_issues_no_subprocess_call(tmp_path, monkeypatch):
    ctx, task_svc, ids = _seed(tmp_path)
    conductor_api = _state_client(ctx, monkeypatch)

    calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: calls.append((a, k)) or (_ for _ in ()).throw(
            AssertionError("subprocess.run must not be called")))

    conductor_api.state(project="p")
    assert not calls, f"unexpected subprocess calls: {calls}"


def test_work_graph_issues_no_subprocess_call(tmp_path, monkeypatch):
    ctx, task_svc, ids = _seed(tmp_path)
    work_api = _graph_client(ctx, monkeypatch)

    calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: calls.append((a, k)) or (_ for _ in ()).throw(
            AssertionError("subprocess.run must not be called")))

    work_api.work_graph(project="p")
    assert not calls, f"unexpected subprocess calls: {calls}"
