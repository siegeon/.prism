"""Signal loss reads differently from healthy work (task e9625a4d).

A driven task (status=in_progress) whose task-scoped report signal (the
drive_heartbeat channel) has gone genuinely dark for a long while must NOT
render the identical "driver active" pill a freshly-adrift task wears --
the owner watched "DRIVER ACTIVE ... LAST REPORT 5:34 AGO" and "...5:49
AGO" read equally reassuring as the number climbed toward silence.

The distinction is a server-computed READ, never a client-invented cutoff:
api/conductor.py::_with_report_signal (additive, mirrors the _with_claimed
precedent at :53-66) enriches activity.report_signal_lost for 'adrift' rows
by consulting drive_heartbeat.heartbeat_age_s -- the same non-policy module
conductor_service.activity_for already reads. It NEVER edits
conductor_service.py (a control_plane.POLICY_FILES entry -- editing it trips
the candidate-controls-judge tooth) and NEVER widens the settled 120s
task_motion_s / 90s session_quiet_s / 180s HEARTBEAT_WINDOW_S thresholds
(mx-b26121 rejected exactly that). SIGNAL_LOST_AFTER_S is a NEW, additional
threshold *derived from* HEARTBEAT_WINDOW_S, not a fresh unrelated number.

Wording register is "we can't see it", never an alarm/intervention word
(idle, stalled, needs you, attention -- mx-dfd56a, this ticket's own
likely_misfire). The HANDOFF strip must stop claiming a step is merely
"on deck" once the timeline shows it actively running (working/driving/
adrift) -- today it says "ready -> worker on deck for X" regardless of
whether the step has been running for 24 minutes.

The PRISM SPA has NO JS test runner, so UI ACs are pinned by asserting the
ACTUAL TSX source (convention: test_conductor_page_animated_cleanup_ui.py /
test_no_driver_copy_honest_ui.py) -- matching the RENDERED string within its
enclosing branch, never a comment, and never a fixed character window.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "prism_service" / "web" / "src"
CONDUCTOR_SRC = (WEB / "pages" / "ConductorPage.tsx").read_text(encoding="utf-8")
ACTIVITY_LABEL_SRC = (WEB / "lib" / "activityLabel.ts").read_text(encoding="utf-8")


def _activity_label_fn_body() -> str:
    """The body of activityLabel(), the ONE function that renders the
    we-can't-see-it wording -- scoped narrowly so a blanket module-text
    search doesn't false-positive on isSignalLost()'s legitimate 'adrift'/
    'stalled' state-name literals (the exact false-positive concern this
    suite's own docstring already flags for fmtIdle/the stalled pill)."""
    start = ACTIVITY_LABEL_SRC.index("export function activityLabel(")
    end = ACTIVITY_LABEL_SRC.index("\n}", start)
    return ACTIVITY_LABEL_SRC[start:end]


# ---------------------------------------------------------------------------
# Backend: api/conductor.py additive enrichment
# ---------------------------------------------------------------------------

def _age_heartbeat(scores_db: str, task_id: str, seconds_ago: float) -> None:
    """Rewrite a recorded heartbeat's last_progress_at into the past (mirrors
    tests/unit/test_drive_heartbeat_activity.py's own helper) so a staleness
    test doesn't need to sleep out the real window."""
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    conn = sqlite3.connect(scores_db)
    conn.execute(
        "UPDATE drive_heartbeats SET last_progress_at = ? WHERE task_id = ?",
        (ts, task_id),
    )
    conn.commit()
    conn.close()


def test_signal_lost_threshold_derives_from_settled_heartbeat_window():
    """SIGNAL_LOST_AFTER_S must be a NEW threshold derived FROM
    drive_heartbeat.HEARTBEAT_WINDOW_S, never a fresh unrelated magic number
    and never a redefinition of the settled 180s constant itself
    (mx-b26121)."""
    from prism_service.api import conductor as conductor_api
    from prism_service.services import drive_heartbeat

    assert drive_heartbeat.HEARTBEAT_WINDOW_S == 180, \
        "the settled 180s driving window must not move"
    assert conductor_api.SIGNAL_LOST_AFTER_S > drive_heartbeat.HEARTBEAT_WINDOW_S, \
        "signal-loss must be a LONGER silence than the ordinary driving window"


def test_with_report_signal_flags_long_task_scoped_silence_as_lost(tmp_path):
    """A heartbeat that exists but has not reported in a long while (well
    past SIGNAL_LOST_AFTER_S) marks the adrift row's activity as
    report_signal_lost -- the genuine "we can't see it" case."""
    from prism_service.api import conductor as conductor_api
    from prism_service.services import drive_heartbeat

    scores_db = str(tmp_path / "scores.db")
    task_id = "task-long-silent"
    beat = drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": task_id, "step": "write_failing_tests",
        "elapsed_s": 100, "last_tool": "Bash", "work_units": 1,
    })
    assert beat.get("ok") is True, beat
    _age_heartbeat(scores_db, task_id, seconds_ago=conductor_api.SIGNAL_LOST_AFTER_S + 60)

    managed = [{"id": task_id, "title": "t", "activity": {
        "state": "adrift", "task_motion_s": 900, "session_quiet_s": 40,
    }}]
    out = conductor_api._with_report_signal(managed, scores_db)
    assert out[0]["activity"]["report_signal_lost"] is True, out


def test_with_report_signal_leaves_a_freshly_adrift_task_alone(tmp_path):
    """A heartbeat that only just fell outside the 180s driving window (e.g.
    200s) must NOT read as signal-lost -- this is the normal, healthy middle
    of a step, not long-term silence."""
    from prism_service.api import conductor as conductor_api
    from prism_service.services import drive_heartbeat

    scores_db = str(tmp_path / "scores.db")
    task_id = "task-freshly-adrift"
    beat = drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": task_id, "step": "write_failing_tests",
        "elapsed_s": 100, "last_tool": "Bash", "work_units": 1,
    })
    assert beat.get("ok") is True, beat
    _age_heartbeat(scores_db, task_id, seconds_ago=drive_heartbeat.HEARTBEAT_WINDOW_S + 20)

    managed = [{"id": task_id, "title": "t", "activity": {
        "state": "adrift", "task_motion_s": 200, "session_quiet_s": 40,
    }}]
    out = conductor_api._with_report_signal(managed, scores_db)
    assert out[0]["activity"]["report_signal_lost"] is False, out


def test_with_report_signal_flags_adrift_with_no_heartbeat_ever_as_lost(tmp_path):
    """No task-scoped report has EVER arrived (heartbeat_age_s is None) for
    a task the board reports as adrift -- that is signal loss too, not a
    freshly-started step (adrift already implies >120s past the last real
    transition, well past 'first beat immediately' doctrine)."""
    from prism_service.api import conductor as conductor_api

    scores_db = str(tmp_path / "scores.db")
    managed = [{"id": "task-never-reported", "title": "t", "activity": {
        "state": "adrift", "task_motion_s": 500, "session_quiet_s": 40,
    }}]
    out = conductor_api._with_report_signal(managed, scores_db)
    assert out[0]["activity"]["report_signal_lost"] is True, out


def test_with_report_signal_ignores_non_adrift_states(tmp_path):
    """The enrichment is scoped to 'adrift'/'stalled' rows only -- working/
    driving/paused already carry their own honest wording and must not gain
    this key at all.

    SUPERSESSION (task 0f090a6c, 2026-08-15): 'stalled' was originally
    excluded here too ("working/driving/stalled/paused already carry their
    own honest wording") -- that was the bug: 'stalled' is the OTHER
    claimed-but-unobservable classification alongside 'adrift' and needed
    the identical treatment. See
    tests/unit/test_invisible_worker_states_read_cannot_see.py for the
    positive coverage of 'stalled' now gaining report_signal_lost."""
    from prism_service.api import conductor as conductor_api

    scores_db = str(tmp_path / "scores.db")
    for state in ("working", "driving", "paused", "awaiting_gate"):
        managed = [{"id": f"task-{state}", "title": "t", "activity": {"state": state}}]
        out = conductor_api._with_report_signal(managed, scores_db)
        assert "report_signal_lost" not in (out[0].get("activity") or {}), (state, out)


# ---------------------------------------------------------------------------
# Frontend: ConductorPage.tsx TaskTile pill + HANDOFF strip
# ---------------------------------------------------------------------------

def _extract_between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    j = src.index(end, i + len(start))
    return src[i:j]


def test_pill_reads_the_server_computed_report_signal_lost_field():
    """The client reads the server field by name -- never invents its own
    staleness cutoff from a raw timestamp (stop_if).

    SUPERSESSION (task 0f090a6c, 2026-08-15): the read used to be an inline
    `task.activity?.report_signal_lost` ternary in ConductorPage.tsx itself;
    it now lives in the ONE shared decision, lib/activityLabel.ts's
    isSignalLost(), which ConductorPage.tsx calls via activityLabel()
    rather than re-deriving the branch locally (mx-d412e0 precedent) --
    see test_invisible_worker_states_read_cannot_see.py's
    test_conductor_page_consumes_the_shared_activity_label_module."""
    assert "report_signal_lost" in ACTIVITY_LABEL_SRC, (
        "the shared activityLabel module must branch on the server-computed "
        "activity.report_signal_lost field, not a client-side heuristic")
    assert "activityLabel" in CONDUCTOR_SRC and "lib/activityLabel" in CONDUCTOR_SRC, (
        "ConductorPage.tsx must consume the shared module rather than "
        "re-deriving the report_signal_lost branch inline")


def test_signal_lost_pill_wording_differs_from_driver_active():
    """The signal-lost wording renders different leading text than
    'driver active' -- the ticket's own captured symptom is that both read
    identically reassuring as staleness grows.

    SUPERSESSION (task 0f090a6c, 2026-08-15): the wording now lives in
    lib/activityLabel.ts's activityLabel() (the shared decision), not
    inline in ConductorPage.tsx -- scoped to that function's own body so
    the check isn't defeated by 'driver active' legitimately appearing
    elsewhere in ConductorPage.tsx's OTHER (non-signal-lost) branches."""
    branch = _activity_label_fn_body()
    assert "driver active" not in branch, (
        "activityLabel()'s we-can't-see-it branch must not reuse the "
        f"'driver active' wording verbatim: {branch!r}")
    assert branch.strip() != "", "activityLabel() must render something"


def test_signal_lost_wording_carries_no_alarm_vocabulary():
    """mx-dfd56a: idle/stalled/needs-you/attention are INTERVENTION words --
    reserved for states where the owner truly has something to click. Scoped
    to activityLabel()'s own function body (fmtIdle / the stalled pill
    legitimately use 'idle' elsewhere, and isSignalLost()'s SIGNAL_LOST_STATES
    set legitimately contains the literal state name 'stalled' -- both must
    not false-positive this check).

    SUPERSESSION (task 0f090a6c, 2026-08-15): re-scoped from a CONDUCTOR_SRC
    character-window extraction (assumed the OLD inline adjacency) to the
    shared activityLabel() function body in lib/activityLabel.ts."""
    branch = _activity_label_fn_body()
    lowered = branch.lower()
    for bad in ("idle", "stalled", "needs you", "attention"):
        assert bad not in lowered, (
            f"activityLabel()'s we-can't-see-it wording must not say "
            f"{bad!r} (mx-dfd56a): {branch!r}")


def test_handoff_never_says_on_deck_for_a_step_already_running():
    """The HANDOFF strip currently says 'ready -> worker on deck for X'
    whenever curStep resolves, independent of whether the timeline shows the
    step actually running (working/driving/adrift) -- a task 24 minutes into
    write_failing_tests renders the identical on-deck wording as a task that
    has never been touched. A guard checking actWorking must be consulted
    BEFORE the on-deck fallback."""
    assert "curStep && actWorking" in CONDUCTOR_SRC, (
        "the HANDOFF ternary must gate on curStep together with actWorking "
        "before falling through to the on-deck phrasing")
    assert "driving → ${worker} running ${curStep.label}" in CONDUCTOR_SRC, (
        "an actively-running step must hand off with running/driving "
        "phrasing, not 'on deck'")
    assert CONDUCTOR_SRC.index("curStep && actWorking") < CONDUCTOR_SRC.index("on deck for"), (
        "the actively-running branch must be checked BEFORE the on-deck "
        "fallback in the ternary chain, or a running step still falls "
        "through to on-deck wording")
