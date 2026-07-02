"""RED — conductor tiles share turns across tasks (task 03a8bfe3).

Owner-reported live bug: all concurrent conductor tiles render IDENTICAL
turns/burn. phase_progress sums each linked session's ENTIRE transcript
(claude_transcripts.live_tokens_for_session / live_token_events_for_session)
and every task driven by ONE Claude session links the SAME session id in
task_sessions — so N concurrent tasks all report the whole-session totals.

Contract pinned here (plan AC-1..AC-4):
  * A session SHARED by several tasks is intersected with each task's WORK
    WINDOW — start = the task's first history timestamp (_task_window_start),
    end = completed_at for done/cancelled tasks else now — so two concurrent
    tasks report DIFFERENT turn sets / burn graphs / token totals (AC-1) and
    a whole-session session_outcomes.tokens_used row never leaks into a
    shared task's windowed total (AC-2).
  * An EXCLUSIVE single-task session keeps its full span, including turns
    recorded BEFORE the task's first history row — the #137 young-task
    semantics are preserved verbatim (AC-3).
  * The windowed read lives in claude_transcripts as
    live_token_events_for_session_windowed (flat + nested subagent
    transcripts, inclusive bounds, [] on an inverted window) so the per-file
    (mtime,size) event cache is reused across the N tasks windowing one
    shared session per poll (AC-4).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import claude_transcripts as ct  # noqa: E402

_PID = "task_windowing_pid"


def _write_transcript(path: Path, turns: list[tuple[float, int]]) -> None:
    """Write a Claude transcript JSONL with one assistant turn per (epoch, out)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    lines = []
    for ep, tok in turns:
        ts = datetime.fromtimestamp(ep, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        lines.append(json.dumps({
            "type": "assistant",
            "timestamp": ts,
            "message": {"usage": {"output_tokens": tok}},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _conductor(tmp_path):
    """ConductorService whose scores.db lives at <tmp>/projects/<pid>/scores.db
    so phase_progress derives project_id == _PID for the resolvers. Mirrors
    tests/unit/test_phase_progress_token_turns.py."""
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    proj_dir = tmp_path / "projects" / _PID
    proj_dir.mkdir(parents=True, exist_ok=True)
    scores_db = str(proj_dir / "scores.db")
    task_svc = TaskService(str(proj_dir / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    conn = sqlite3.connect(scores_db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_outcomes ("
            "session_id TEXT PRIMARY KEY, duration_s REAL, tokens_used INTEGER, "
            "files_read INTEGER, files_modified INTEGER, skills_invoked INTEGER)"
        )
        conn.commit()
    finally:
        conn.close()
    return task_svc, cond


def _patch_resolvers(monkeypatch, source_path: str, override_dir: str):
    """Bind the phase_progress resolvers at their module homes (the real seam)."""
    monkeypatch.setattr(ct, "_project_source_path", lambda pid: source_path)
    from prism_service.services import claude_memory as cm
    monkeypatch.setattr(cm, "configured_project_dir", lambda pid: override_dir)


def _two_tasks_one_session(tmp_path):
    """One session JSONL, two tasks with DISJOINT work windows.

    Timeline (all real wall-clock, separated by short sleeps so the task
    history rows bracket disjoint windows):
      task1 created+advanced -> [win1 opens]
      task1 done             -> [win1 closes at completed_at]
      task2 created+advanced -> [win2 opens .. now]
    Then the shared transcript gets 3 turns inside win1 (100+110+120) and
    2 turns inside win2 (500+600).
    """
    task_svc, cond = _conductor(tmp_path)
    sid = "55555555-5555-5555-5555-555555555555"

    t1 = task_svc.create(title="task one (earlier window)")
    cond.advance_task(t1.id)
    time.sleep(0.05)
    task_svc.update(t1.id, status="done")
    time.sleep(0.05)
    t2 = task_svc.create(title="task two (later window)")
    cond.advance_task(t2.id)
    task_svc.update(t2.id, status="in_progress")

    task_svc.link_session(t1.id, sid)
    task_svc.link_session(t2.id, sid)

    win1_start = cond._task_window_start(t1.id)
    win1_end = cond._parse_iso(task_svc.get(t1.id).completed_at)
    assert win1_end is not None and win1_end > win1_start
    win2_start = cond._task_window_start(t2.id)
    assert win2_start > win1_end

    turns1 = [(win1_start + (win1_end - win1_start) * f, tok)
              for f, tok in ((0.25, 100), (0.5, 110), (0.75, 120))]
    turns2 = [(win2_start + 0.005, 500), (win2_start + 0.010, 600)]
    d = tmp_path / "claudedir"
    _write_transcript(d / f"{sid}.jsonl", turns1 + turns2)
    return task_svc, cond, t1, t2, sid, d


# ----------------------------------------------------------------------
# AC-1 — two tasks sharing ONE session report their OWN windowed numbers.
# ----------------------------------------------------------------------

def test_shared_session_two_tasks_window_their_own_turns(tmp_path, monkeypatch):
    _, cond, t1, t2, _sid, d = _two_tasks_one_session(tmp_path)
    _patch_resolvers(monkeypatch, source_path="", override_dir=str(d))

    pp1 = cond.phase_progress(t1.id)
    pp2 = cond.phase_progress(t2.id)

    assert pp1["turns"] == 3, (
        f"task1 must report ONLY its own 3 in-window turns, got {pp1['turns']} "
        "(whole-session bleed — the shared-tile bug)"
    )
    assert pp2["turns"] == 2, (
        f"task2 must report ONLY its own 2 in-window turns, got {pp2['turns']}"
    )
    assert [t["out"] for t in pp1["token_turns"]] == [100, 110, 120]
    assert [t["out"] for t in pp2["token_turns"]] == [500, 600]
    assert pp1["tokens_since_step"] == 330, pp1["tokens_since_step"]
    assert pp2["tokens_since_step"] == 1100, pp2["tokens_since_step"]
    assert pp1["token_turns"] != pp2["token_turns"], (
        "concurrent tiles must not render IDENTICAL burn graphs"
    )


# ----------------------------------------------------------------------
# AC-2 — whole-session outcome tokens must NOT leak into a shared task's
# windowed total (max()-ing session_outcomes.tokens_used back in would
# resurrect the bug the moment the post-hoc importer lands).
# ----------------------------------------------------------------------

def test_shared_session_outcome_tokens_do_not_leak(tmp_path, monkeypatch):
    _, cond, t1, t2, sid, d = _two_tasks_one_session(tmp_path)
    scores_db = str(tmp_path / "projects" / _PID / "scores.db")
    conn = sqlite3.connect(scores_db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO session_outcomes "
            "(session_id, duration_s, tokens_used, files_read, files_modified, "
            "skills_invoked) VALUES (?, 0, 999999, 0, 0, 0)",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()
    _patch_resolvers(monkeypatch, source_path="", override_dir=str(d))

    pp1 = cond.phase_progress(t1.id)
    pp2 = cond.phase_progress(t2.id)
    assert pp1["tokens_since_step"] == 330, (
        f"whole-session outcome tokens leaked into task1's shared window: "
        f"{pp1['tokens_since_step']}"
    )
    assert pp2["tokens_since_step"] == 1100, (
        f"whole-session outcome tokens leaked into task2's shared window: "
        f"{pp2['tokens_since_step']}"
    )


# ----------------------------------------------------------------------
# AC-3 — an EXCLUSIVE session keeps its FULL span (turns before the task's
# first history row still count): #137 young-task semantics preserved.
# ----------------------------------------------------------------------

def test_exclusive_session_keeps_full_span(tmp_path, monkeypatch):
    task_svc, cond = _conductor(tmp_path)
    sid = "66666666-6666-6666-6666-666666666666"
    d = tmp_path / "claudedir"
    now = time.time()
    # Work already on disk BEFORE the task exists (just-linked young task).
    _write_transcript(d / f"{sid}.jsonl", [(now - 500, 80), (now - 400, 90)])

    t = task_svc.create(title="young task, exclusive session")
    cond.advance_task(t.id)
    task_svc.link_session(t.id, sid)
    _patch_resolvers(monkeypatch, source_path="", override_dir=str(d))

    pp = cond.phase_progress(t.id)
    assert pp["turns"] == 2, (
        f"an exclusive session must keep its full span (#137 young-task "
        f"semantics) — got {pp['turns']} turns"
    )
    assert pp["tokens_since_step"] == 170, pp["tokens_since_step"]


# ----------------------------------------------------------------------
# AC-4 — the windowed reader lives in claude_transcripts and reuses the
# flat + nested subagent layout with inclusive bounds.
# ----------------------------------------------------------------------

def test_windowed_reader_filters_events(tmp_path):
    assert hasattr(ct, "live_token_events_for_session_windowed"), (
        "claude_transcripts must expose live_token_events_for_session_windowed "
        "(the windowed per-turn read the conductor windows tasks through)"
    )
    sid = "77777777-7777-7777-7777-777777777777"
    d = tmp_path / "claudedir"
    _write_transcript(d / f"{sid}.jsonl", [(1000.0, 10), (2000.0, 20)])
    _write_transcript(d / sid / "subagent.jsonl", [(1500.0, 15), (3000.0, 30)])

    fn = ct.live_token_events_for_session_windowed
    events = fn(sid, "", 1000.0, 2000.0, override_dir=str(d))
    assert [tok for _, tok in events] == [10, 15, 20], (
        f"inclusive [since, until] over flat+nested transcripts, got {events}"
    )
    assert fn(sid, "", 2000.0, 1000.0, override_dir=str(d)) == [], (
        "inverted window must return []"
    )
