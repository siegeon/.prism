"""Every invisible-worker state reads "we can't see it" (task 0f090a6c).

Round-2 follow-up on task e9625a4d ("signal loss reads differently"). That
fix scoped its report-signal enrichment to `adrift` rows only
(api/conductor.py::_with_report_signal, line 82: `if activity.get("state")
== "adrift":`) -- so a CLAIMED task whose worker has gone genuinely dark
while classified `stalled` (not `adrift`) never gets the same honest
"we can't see it" treatment: it renders the alarm-toned "no active driver"
regardless of how long the silence has run, and the task-detail page's
StepRail carries NO signal-loss cue at all (api/tasks.py's detail route
attaches raw `cond.activity_for(t, pp)`, never the enrichment).

This suite pins the fix at three layers:

  1. Backend: _with_report_signal must enrich BOTH 'adrift' and 'stalled'
     rows with the SAME report_signal_lost boolean, using the SAME
     drive_heartbeat.heartbeat_age_s primitive (never conductor_service.py,
     a control_plane.POLICY_FILES entry) -- and must attach a NEW numeric
     field (report_signal_age_s) carrying the REAL heartbeat age, so the
     client never has to fall back to task_motion_s (step-transition
     recency) for the elapsed-since-observation number. That fallback is
     the exact bug: ConductorPage.tsx's adrift-signal-lost branch today
     derives its 's ago' number from task.activity.task_motion_s, which can
     read low/fresh while the real heartbeat is null or ancient.
  2. api/tasks.py's task-detail route must carry the same enrichment, so the
     Implementation tab (StepRail.tsx, reached via PlanView.tsx from
     TaskDetailPage.tsx) has the field to render at all -- today it does
     not reach that route.
  3. Frontend: a single new module (web/src/lib/activityLabel.ts) is the
     ONE place that decides the we-can't-see-it wording; ConductorPage.tsx,
     SdlcProgress.tsx and StepRail.tsx must all consume it rather than
     re-deriving the branch locally, so the next classification tweak
     cannot re-diverge across three copies (mx-d412e0 precedent). The SPA
     has no JS test runner, so these are pinned by asserting the ACTUAL
     TSX/TS source (convention: test_conductor_page_animated_cleanup_ui.py),
     matching a RENDERED literal, never a comment.

NOTE for whoever lands the implementation commit: extending
_with_report_signal to 'stalled' rows contradicts
tests/unit/test_conductor_tile_signal_loss_reads_distinctly.py::
test_with_report_signal_ignores_non_adrift_states, which currently asserts
'stalled' rows must NEVER gain report_signal_lost. Retire the 'stalled'
case out of that test's iterated states list, in the implementation commit,
with a comment naming this task as the supersession -- the remaining
states (working/driving/paused/awaiting_gate) are still correctly excluded
and must keep passing unchanged. The same implementation commit also needs
to update test_pill_reads_the_server_computed_report_signal_lost_field /
test_signal_lost_pill_wording_differs_from_driver_active /
test_signal_lost_wording_carries_no_alarm_vocabulary in that file once
ConductorPage.tsx's inline ternary is replaced by the shared
activityLabel() call -- their _extract_between anchors assume the OLD
inline adjacency.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "prism_service" / "web" / "src"
CONDUCTOR_SRC = (WEB / "pages" / "ConductorPage.tsx").read_text(encoding="utf-8")
SDLC_SRC = (WEB / "components" / "conductor" / "SdlcProgress.tsx").read_text(encoding="utf-8")
STEPRAIL_SRC = (WEB / "components" / "conductor" / "StepRail.tsx").read_text(encoding="utf-8")
ACTIVITY_LABEL_PATH = WEB / "lib" / "activityLabel.ts"


def _age_heartbeat(scores_db: str, task_id: str, seconds_ago: float) -> None:
    """Rewrite a recorded heartbeat's last_progress_at into the past (mirrors
    test_conductor_tile_signal_loss_reads_distinctly.py's own helper)."""
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    conn = sqlite3.connect(scores_db)
    conn.execute(
        "UPDATE drive_heartbeats SET last_progress_at = ? WHERE task_id = ?",
        (ts, task_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. Backend: _with_report_signal must cover 'stalled' too
# ---------------------------------------------------------------------------

def test_with_report_signal_flags_long_silent_stalled_row_as_lost(tmp_path):
    """A CLAIMED task classified 'stalled' whose heartbeat has been silent
    well past SIGNAL_LOST_AFTER_S must be flagged report_signal_lost=True --
    today the enrichment skips 'stalled' entirely (scoped to 'adrift' only),
    so this key is simply absent."""
    from prism_service.api import conductor as conductor_api
    from prism_service.services import drive_heartbeat

    scores_db = str(tmp_path / "scores.db")
    task_id = "task-stalled-long-silent"
    beat = drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": task_id, "step": "write_failing_tests",
        "elapsed_s": 100, "last_tool": "Bash", "work_units": 1,
    })
    assert beat.get("ok") is True, beat
    _age_heartbeat(scores_db, task_id, seconds_ago=conductor_api.SIGNAL_LOST_AFTER_S + 60)

    managed = [{"id": task_id, "title": "t", "activity": {
        "state": "stalled", "task_motion_s": 900, "session_quiet_s": 900,
    }}]
    out = conductor_api._with_report_signal(managed, scores_db)
    assert out[0]["activity"].get("report_signal_lost") is True, out


def test_with_report_signal_flags_never_reported_stalled_row_as_lost(tmp_path):
    """No task-scoped heartbeat has EVER arrived (heartbeat_age_s is None)
    for a task the board reports as 'stalled' -- signal loss too, not merely
    'no driver was ever attached'."""
    from prism_service.api import conductor as conductor_api

    scores_db = str(tmp_path / "scores.db")
    managed = [{"id": "task-stalled-never-reported", "title": "t", "activity": {
        "state": "stalled", "task_motion_s": 500, "session_quiet_s": 500,
    }}]
    out = conductor_api._with_report_signal(managed, scores_db)
    assert out[0]["activity"].get("report_signal_lost") is True, out


def test_with_report_signal_leaves_a_freshly_stalled_row_alone(tmp_path):
    """A heartbeat that fell outside the 180s driving window but NOT yet
    past SIGNAL_LOST_AFTER_S (540s) is a real, observable short silence --
    the row stays report_signal_lost=False and keeps its ordinary
    'no active driver' wording; only long-term darkness earns the neutral
    we-can't-see-it register."""
    from prism_service.api import conductor as conductor_api
    from prism_service.services import drive_heartbeat

    scores_db = str(tmp_path / "scores.db")
    task_id = "task-freshly-stalled"
    beat = drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": task_id, "step": "write_failing_tests",
        "elapsed_s": 100, "last_tool": "Bash", "work_units": 1,
    })
    assert beat.get("ok") is True, beat
    _age_heartbeat(scores_db, task_id, seconds_ago=drive_heartbeat.HEARTBEAT_WINDOW_S + 20)

    managed = [{"id": task_id, "title": "t", "activity": {
        "state": "stalled", "task_motion_s": 200, "session_quiet_s": 200,
    }}]
    out = conductor_api._with_report_signal(managed, scores_db)
    assert out[0]["activity"].get("report_signal_lost") is False, out


def test_with_report_signal_attaches_real_heartbeat_age_distinct_from_task_motion(tmp_path):
    """The elapsed-since-last-observation number must come from the REAL
    heartbeat age (drive_heartbeat.heartbeat_age_s), a NEW field
    report_signal_age_s -- never from task_motion_s (step-transition
    recency), which is a different clock that can read low while the real
    heartbeat is ancient. This is the label's internal-inconsistency bug
    the ticket exists to fix."""
    from prism_service.api import conductor as conductor_api
    from prism_service.services import drive_heartbeat

    scores_db = str(tmp_path / "scores.db")
    task_id = "task-distinct-clocks"
    beat = drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": task_id, "step": "write_failing_tests",
        "elapsed_s": 100, "last_tool": "Bash", "work_units": 1,
    })
    assert beat.get("ok") is True, beat
    real_age = conductor_api.SIGNAL_LOST_AFTER_S + 300
    _age_heartbeat(scores_db, task_id, seconds_ago=real_age)

    # task_motion_s deliberately set to a SMALL, fresh-looking number so a
    # correct fix cannot accidentally source the elapsed figure from it.
    managed = [{"id": task_id, "title": "t", "activity": {
        "state": "stalled", "task_motion_s": 5, "session_quiet_s": 900,
    }}]
    out = conductor_api._with_report_signal(managed, scores_db)
    activity = out[0]["activity"]
    assert activity.get("report_signal_lost") is True, activity
    age = activity.get("report_signal_age_s")
    assert age is not None, activity
    assert abs(age - real_age) < 5, (
        f"report_signal_age_s ({age}) must track the REAL heartbeat age "
        f"({real_age}), not task_motion_s (5)")


def test_with_report_signal_age_is_none_when_never_reported(tmp_path):
    """When no heartbeat exists at all, report_signal_age_s reads None (an
    honest 'never observed'), never a fabricated 0 or the task_motion_s
    value."""
    from prism_service.api import conductor as conductor_api

    scores_db = str(tmp_path / "scores.db")
    managed = [{"id": "task-age-none", "title": "t", "activity": {
        "state": "adrift", "task_motion_s": 500, "session_quiet_s": 40,
    }}]
    out = conductor_api._with_report_signal(managed, scores_db)
    activity = out[0]["activity"]
    assert activity.get("report_signal_lost") is True, activity
    assert activity.get("report_signal_age_s") is None, activity


# ---------------------------------------------------------------------------
# 2. Backend: the task-detail route (api/tasks.py) must carry the enrichment
# ---------------------------------------------------------------------------

class _FakeTaskSvc:
    def __init__(self, task):
        self._task = task

    def get(self, _tid):
        return self._task

    def history(self, _tid):
        return []

    def sessions_for_task(self, _tid):
        return []


class _FakeCond:
    """Stands in for ConductorService: activity_for returns the RAW,
    unenriched activity dict -- exactly like the real activity_for, which
    knows nothing about drive_heartbeat's report-signal enrichment (that
    lives one layer up, in api/conductor.py). Proves the detail route
    itself must apply the enrichment, not lean on activity_for to do it."""

    def __init__(self, activity: dict):
        self._activity = activity

    def phase_progress(self, _task_id):
        return {}

    def activity_for(self, _t, _pp):
        return dict(self._activity)


class _FakeCtx:
    def __init__(self, task_svc, cond, data_dir):
        self.task_svc = task_svc
        self.conductor_svc = cond
        self._data_dir = data_dir


def _detail_client(monkeypatch, ctx):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api

    monkeypatch.setattr(tasks_api, "get_project", lambda _p: ctx)
    # Skip the transcript-parsing attaches entirely -- irrelevant to this
    # test and expensive/fragile against a fake empty session list.
    monkeypatch.setattr(tasks_api, "_attach_turn_tokens", lambda *a, **k: None)
    monkeypatch.setattr(tasks_api, "_step_token_totals", lambda *a, **k: {})
    monkeypatch.setattr(tasks_api, "_attach_spend", lambda *a, **k: None)
    monkeypatch.setattr(tasks_api, "_build_timeline", lambda *a, **k: {"lanes": [], "gates": []})

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def test_task_detail_route_carries_report_signal_enrichment_for_stalled(tmp_path, monkeypatch):
    """GET /api/tasks/{id} must carry activity.report_signal_lost for a
    long-silent 'stalled' task -- today the route attaches
    cond.activity_for(t, pp) raw (api/tasks.py ~line 778), so this key never
    reaches the Implementation tab's StepRail at all."""
    from prism_service.api import conductor as conductor_api
    from prism_service.services import drive_heartbeat

    scores_db = tmp_path / "scores.db"
    task_id = "t-detail-stalled"
    beat = drive_heartbeat.record_heartbeat(str(scores_db), {
        "task_id": task_id, "step": "write_failing_tests",
        "elapsed_s": 100, "last_tool": "Bash", "work_units": 1,
    })
    assert beat.get("ok") is True, beat
    _age_heartbeat(str(scores_db), task_id, seconds_ago=conductor_api.SIGNAL_LOST_AFTER_S + 90)

    task = {"id": task_id, "title": "t", "status": "in_progress", "priority": 0}
    cond = _FakeCond({"state": "stalled", "task_motion_s": 900, "session_quiet_s": 900})
    ctx = _FakeCtx(_FakeTaskSvc(task), cond, tmp_path)

    client = _detail_client(monkeypatch, ctx)
    r = client.get(f"/api/tasks/{task_id}?project=proj")
    assert r.status_code == 200, r.text
    body = r.json()
    activity = body.get("activity") or {}
    assert activity.get("state") == "stalled", activity
    assert activity.get("report_signal_lost") is True, (
        f"the detail route must enrich activity with report_signal_lost, got {activity}")
    assert activity.get("report_signal_age_s") is not None, activity


def test_task_detail_route_leaves_a_working_task_activity_unenriched(tmp_path, monkeypatch):
    """The enrichment must not fabricate a report_signal_lost key for states
    where it is meaningless (working) -- scoped exactly like the board-state
    enrichment."""
    task = {"id": "t-detail-working", "title": "t", "status": "in_progress", "priority": 0}
    cond = _FakeCond({"state": "working", "task_motion_s": 5, "session_quiet_s": 5})
    ctx = _FakeCtx(_FakeTaskSvc(task), cond, tmp_path)

    client = _detail_client(monkeypatch, ctx)
    r = client.get("/api/tasks/t-detail-working?project=proj")
    assert r.status_code == 200, r.text
    activity = r.json().get("activity") or {}
    assert "report_signal_lost" not in activity, activity


# ---------------------------------------------------------------------------
# 3. Frontend: one consolidated module, consumed by all three surfaces
# ---------------------------------------------------------------------------

def test_activity_label_module_exists():
    assert ACTIVITY_LABEL_PATH.exists(), (
        "web/src/lib/activityLabel.ts must exist as the single source of "
        "the we-can't-see-it label decision (mirrors the useConductorState "
        "single-source-hook precedent, mx-d412e0)")


def _activity_label_src() -> str:
    return ACTIVITY_LABEL_PATH.read_text(encoding="utf-8")


def test_activity_label_module_covers_both_adrift_and_stalled():
    src = _activity_label_src()
    assert '"adrift"' in src and '"stalled"' in src, (
        "the shared decision must branch on BOTH classifications, not just "
        "'adrift' (the bug this ticket exists to fix)")
    assert "report_signal_lost" in src, (
        "must read the server-computed field, never invent a client cutoff")


def test_activity_label_module_uses_real_heartbeat_age_not_task_motion():
    src = _activity_label_src()
    assert "report_signal_age_s" in src, (
        "the elapsed-since-observation number must come from the real "
        "heartbeat age field, not task_motion_s")


def test_activity_label_module_never_reads_no_active_driver_when_unobservable():
    """The label the module renders while unobservable must not be the
    'no active driver' alarm phrase -- distinguishes 'we can't see it' from
    the genuine no-driver signal."""
    src = _activity_label_src()
    lowered = src.lower()
    assert "we can't see it" in lowered or "we cannot see it" in lowered, (
        "must render the distinct neutral we-can't-see-it wording")


def test_conductor_page_consumes_the_shared_activity_label_module():
    assert "activityLabel" in CONDUCTOR_SRC and "lib/activityLabel" in CONDUCTOR_SRC, (
        "ConductorPage.tsx must import the shared decision rather than "
        "re-deriving the we-can't-see-it branch locally")


def test_sdlc_progress_consumes_the_shared_activity_label_module():
    assert "activityLabel" in SDLC_SRC and "lib/activityLabel" in SDLC_SRC, (
        "SdlcProgress.tsx's ACTIVITY_META-driven stateLabel must defer to "
        "the shared module for the we-can't-see-it case so it cannot "
        "re-diverge from ConductorPage.tsx's tile pill")


def test_step_rail_renders_the_we_cannot_see_it_cue():
    """StepRail.tsx (the task-detail Implementation tab's per-step rail)
    today renders NO activity-state label at all -- only a boolean pulse
    keyed off state==='working'. It must import the shared module and
    surface the we-can't-see-it wording for the current step row, so the
    detail page carries the same cue the conductor tile carries."""
    assert "activityLabel" in STEPRAIL_SRC and "lib/activityLabel" in STEPRAIL_SRC, (
        "StepRail.tsx must consume the shared activityLabel module")
    assert "report_signal_lost" in STEPRAIL_SRC or "activityLabel(" in STEPRAIL_SRC, (
        "StepRail.tsx must actually branch on the unobservable signal, not "
        "just import the module unused")
