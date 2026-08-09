"""RED scaffold -- task pages stop overfetching (task 18a2f89f).

Pins the acceptance criteria for the GET /api/tasks/{id} render path and its
two nearby list surfaces going from an N+1 walk to bounded, indexed queries:

  - TaskService.advance_rows_all() exists as ONE indexed query over
    task_history WHERE action='advance_task', grouped by task_id.
  - ConductorService._median_step_s / _per_step_typical consume it instead of
    looping list() + history(t.id) per task (was ~470 SQL statements/render).
  - The conductor_service.py child-lookup call sites (adjudicate_green_gate,
    gate_decide x2, phase_progress, _child_task_ids, _task_motion_s,
    _children) and api/conductor.py's gate_readiness use
    TaskService.list(parent_id=...) onto idx_tasks_parent instead of loading
    every task on the board and filtering in Python.
  - claude_transcripts.project_token_events_in_window is served from a merged
    sorted event index behind a 5s TTL instead of re-globbing every
    transcript file on every call.
  - main.py's lifespan warms that index at boot.
  - CompletedTasksPage.tsx / ExplorePage.tsx request only their needed
    columns via fields= instead of the full unprojected task payload.

ALL FAIL today (confirmed against task worktree HEAD c5b048d == origin/main):
advance_rows_all does not exist, the SQL-count assertions below observe the
O(N) per-task scans directly, the child-lookup sites still call
self._task_svc.list() with no parent_id, claude_transcripts has no TTL index,
main.py never warms one, and both TSX pages fetch the full task payload.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SRC = _SERVICE_ROOT / "prism_service"


# ----------------------------------------------------------------------
# Fixtures / helpers
# ----------------------------------------------------------------------

def _fresh_task_svc(tmp_path, name="tasks"):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / f"{name}.db"))


def _task_with_history(task_svc, n_advances, parent_id=""):
    """Create a task and append n_advances 'advance_task' history rows."""
    t = task_svc.create(title="w", parent_id=parent_id)
    for i in range(n_advances):
        task_svc.record_history(t.id, "advance_task", details=f"to=step{i}")
    return t.id


def _sql_calls(conn, fn, *a, **kw):
    """Run fn(*a, **kw) with a trace callback on conn; return (result, sqls)."""
    calls: list[str] = []
    conn.set_trace_callback(lambda sql: calls.append(sql))
    try:
        result = fn(*a, **kw)
    finally:
        conn.set_trace_callback(None)
    return result, calls


def _conductor(task_svc, scores_db):
    from prism_service.services.conductor_service import ConductorService
    return ConductorService(scores_db, enable_engine=False, task_svc=task_svc)


def _function_body(src: str, name: str) -> str:
    """Slice one function/method body out of a source file by name, from its
    `def name(` to the next `def ` at the SAME indent level. Robust to
    line-number drift elsewhere in the file (unlike hard-coded line ranges)."""
    lines = src.splitlines()
    start = None
    indent = None
    pat = re.compile(r"(\s*)(?:async )?def " + re.escape(name) + r"\(")
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m:
            start, indent = i, m.group(1)
            break
    assert start is not None, f"could not find def {name}( in source"
    end = len(lines)
    end_pat = re.compile(indent + r"(?:async )?def \w+\(")
    for i in range(start + 1, len(lines)):
        if end_pat.match(lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


_CONDUCTOR_SRC = (_SRC / "services" / "conductor_service.py").read_text(encoding="utf-8")
_API_CONDUCTOR_SRC = (_SRC / "api" / "conductor.py").read_text(encoding="utf-8")
_MAIN_SRC = (_SRC / "main.py").read_text(encoding="utf-8")
_COMPLETED_TSX = (_SRC / "web" / "src" / "pages" / "CompletedTasksPage.tsx").read_text(
    encoding="utf-8")
_EXPLORE_TSX = (_SRC / "web" / "src" / "pages" / "ExplorePage.tsx").read_text(
    encoding="utf-8")

_TASKS_FETCH_RE = re.compile(
    r"api\s*\.\s*get<[^>]*>\(\s*`(/api/tasks\?project=\$\{project\}[^`]*)`")


# ----------------------------------------------------------------------
# Group A -- TaskService.advance_rows_all: ONE indexed query, grouped
# ----------------------------------------------------------------------

def test_advance_rows_all_exists_as_one_indexed_query(tmp_path):
    task_svc = _fresh_task_svc(tmp_path)
    ids = [_task_with_history(task_svc, n) for n in (2, 3, 1)]
    assert hasattr(task_svc, "advance_rows_all"), (
        "TaskService.advance_rows_all does not exist yet"
    )
    out, calls = _sql_calls(task_svc._db, task_svc.advance_rows_all)
    history_sql = [c for c in calls if "task_history" in c]
    assert len(history_sql) == 1, (
        f"advance_rows_all should read task_history in ONE query, saw "
        f"{len(history_sql)}: {history_sql}"
    )
    for tid, n in zip(ids, (2, 3, 1)):
        assert tid in out and len(out[tid]) == n
        times = [ts for ts, _ in out[tid]]
        assert times == sorted(times), "rows must be ascending by timestamp"


# ----------------------------------------------------------------------
# Group B -- _median_step_s / _per_step_typical stop N+1ing history()
# ----------------------------------------------------------------------

@pytest.mark.parametrize("method", ["_median_step_s", "_per_step_typical"])
def test_step_stats_do_not_n_plus_one_scan_history(tmp_path, method):
    task_svc = _fresh_task_svc(tmp_path)
    for _ in range(8):
        _task_with_history(task_svc, 3)
    cond = _conductor(task_svc, str(tmp_path / "scores.db"))
    _, calls = _sql_calls(task_svc._db, getattr(cond, method))
    history_sql = [c for c in calls if "task_history" in c]
    assert len(history_sql) <= 1, (
        f"{method} issued {len(history_sql)} task_history queries for 8 "
        f"tasks (want O(1) via advance_rows_all, not one history() call "
        f"per task): {history_sql}"
    )


# ----------------------------------------------------------------------
# Group C -- child-lookup call sites scope onto idx_tasks_parent
# ----------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "adjudicate_green_gate", "gate_decide", "phase_progress",
    "_child_task_ids", "_task_motion_s", "_children",
])
def test_conductor_child_lookups_use_parent_id_scoped_list(name):
    body = _function_body(_CONDUCTOR_SRC, name)
    bare = re.findall(r"_task_svc\.list\(\)", body)
    assert not bare, (
        f"{name} still calls self._task_svc.list() with no parent_id -- "
        f"loads the whole board and filters in Python instead of using "
        f"idx_tasks_parent: {bare}"
    )
    assert "_task_svc.list(parent_id=" in body, (
        f"{name} should scope its child lookup via "
        f"self._task_svc.list(parent_id=...)"
    )


def test_gate_readiness_child_lookup_uses_parent_id_scoped_list():
    body = _function_body(_API_CONDUCTOR_SRC, "gate_readiness")
    bare = re.findall(r"_task_svc\.list\(\)", body)
    assert not bare, (
        f"gate_readiness still calls s._task_svc.list() with no parent_id: "
        f"{bare}"
    )
    assert "_task_svc.list(parent_id=" in body, (
        "gate_readiness should scope its child lookup via "
        "s._task_svc.list(parent_id=...)"
    )


# ----------------------------------------------------------------------
# Group D -- claude_transcripts: TTL-cached merged event index
# ----------------------------------------------------------------------

def _write_transcript(path: Path, turns: list[tuple[float, int]]) -> None:
    from datetime import datetime, timezone
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for ep, tok in turns:
        ts = datetime.fromtimestamp(ep, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z")
        lines.append(json.dumps({
            "type": "assistant", "timestamp": ts,
            "message": {"usage": {"output_tokens": tok}},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_project_token_events_in_window_reuses_ttl_cached_merged_index(
    tmp_path, monkeypatch,
):
    from prism_service.services import claude_transcripts as ct
    d = tmp_path / "transcripts"
    _write_transcript(d / "s1.jsonl", [(1_700_000_000.0, 50), (1_700_000_050.0, 60)])
    calls = {"n": 0}
    orig_rglob = Path.rglob

    def counting_rglob(self, pattern):
        calls["n"] += 1
        return orig_rglob(self, pattern)

    monkeypatch.setattr(Path, "rglob", counting_rglob)
    ct.project_token_events_in_window(
        "unused", 1_699_999_000.0, 1_700_001_000.0, override_dir=str(d))
    first = calls["n"]
    assert first > 0, "sanity: the first call should scan the transcript dir"
    ct.project_token_events_in_window(
        "unused", 1_699_999_000.0, 1_700_001_000.0, override_dir=str(d))
    assert calls["n"] == first, (
        f"a second call within the TTL window re-globbed the transcript dir "
        f"({calls['n']} rglob calls total vs {first} after call 1) -- "
        f"expected a merged sorted event index cached behind a TTL"
    )


def test_claude_transcripts_gains_a_ttl_cached_project_event_index():
    from prism_service.services import claude_transcripts as ct
    assert hasattr(ct, "_project_event_index"), (
        "claude_transcripts._project_event_index (the merged/cached index "
        "builder) does not exist yet"
    )
    assert hasattr(ct, "warm_project_event_index"), (
        "claude_transcripts.warm_project_event_index (the boot-time "
        "warmer) does not exist yet"
    )


# ----------------------------------------------------------------------
# Group E -- main.py warms the transcript event index at boot
# ----------------------------------------------------------------------

def test_lifespan_warms_the_transcript_event_index_at_boot():
    body = _function_body(_MAIN_SRC, "lifespan")
    assert "warm_project_event_index" in body, (
        "main.py's lifespan does not call claude_transcripts."
        "warm_project_event_index at boot"
    )


# ----------------------------------------------------------------------
# Group F -- TSX list pages request a fields= projection, not the payload
# ----------------------------------------------------------------------

def test_completed_tasks_page_requests_a_fields_projection():
    m = _TASKS_FETCH_RE.search(_COMPLETED_TSX)
    assert m, "could not find the /api/tasks fetch call in CompletedTasksPage.tsx"
    assert "fields=" in m.group(1), (
        f"CompletedTasksPage.tsx fetches the full unprojected task payload "
        f"({m.group(1)!r}) instead of a fields= projection"
    )


def test_explore_page_requests_a_fields_projection():
    m = _TASKS_FETCH_RE.search(_EXPLORE_TSX)
    assert m, "could not find the /api/tasks fetch call in ExplorePage.tsx"
    assert "fields=" in m.group(1), (
        f"ExplorePage.tsx fetches the full unprojected task payload "
        f"({m.group(1)!r}) instead of a fields= projection"
    )
