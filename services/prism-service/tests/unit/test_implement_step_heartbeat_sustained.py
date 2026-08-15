"""Harness-guaranteed heartbeat cadence across a long step (task dd1e8871).

Sequel to e3b7ebf6/mx-1a134a/mx-b26121: the drive_heartbeats CHANNEL exists,
but until now its only producer was PROMPT TEXT in
.claude/workflows/implement.js instructing the step agent to self-issue a
curl every 5-8 tool calls -- pure agent discipline, with nothing backstopping
a lapse. On 170991cc that discipline failed mid-step: beats stopped at
01:47 while the session transcript kept growing to 655KB through 02:01, and
activity_for read adrift/stalled for 14+ minutes of genuinely healthy work.

Pins prism_service.services.drive_activity_observer, a SECOND, SERVER-SIDE
producer onto the SAME drive_heartbeats store (additive; drive_heartbeat.py
and conductor_service.py stay read-only -- this module only ever CALLS their
existing public/private methods) that beats ONLY on OBSERVED transcript
growth -- never on elapsed wall-clock alone.

Covers:
  AC-1 -- sustained OBSERVED activity across a step longer than every
          classification window (task_motion_s>120, session_quiet_s>90,
          HEARTBEAT_WINDOW_S=180) keeps the REAL ConductorService.activity_for
          reading driving throughout -- never adrift/stalled.
  AC-2 -- the beat is HARNESS-GUARANTEED: observe_tick is a standalone
          server-side function, not prompt text a step agent must comply
          with.
  AC-3 -- observe_tick beats ONLY when the injected tokens_reader reports
          growth since the previous tick; a session whose transcript has
          stopped growing (simulating a wedged/looping agent) receives NO
          beat, so heartbeat_age_s is free to lapse past HEARTBEAT_WINDOW_S
          exactly as it would with zero producers -- the dead-worker/claim-
          expiry signal survives untouched.
  AC-4 -- a task's first-ever observation never beats (growth needs two
          samples), a task with no linked session is never beaten, and a
          task parked at a gate is never beaten (that seat belongs to the
          distinct adjudicator, not this observer).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


def _cond(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    # Bypass the real project-registry resolution (folder-mode lookups a
    # unit test has no registered project for) by populating the exact
    # cached attributes _project_source_path/_project_override_dir already
    # write on a real daemon. observe_tick only ever reads them through
    # the public methods, so this is a legitimate seam, not a bypass of
    # the behaviour under test.
    cond._src_path_cache = "/fake/source"
    cond._override_dir_cache = ""
    return cond, task_svc, scores_db


def _old_transition(task_svc, task_id, seconds_ago):
    """Insert a stale advance_task history row so task_motion_s reads
    >120s (mirrors test_drive_heartbeat_activity.py's own helper)."""
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    task_svc._db.execute(
        "INSERT INTO task_history (task_id, actor, action, details, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, "", "advance_task", "to=write_failing_tests", ts),
    )
    task_svc._db.commit()


def _driving_task(task_svc, session_id="sess-observed"):
    """A task mid a long step: BOTH the 120s motion window and the 90s
    session-quiet window have already elapsed -- exactly the state a
    sustained OBSERVED-growth beat must rescue from stalled/adrift."""
    t = task_svc.create(title="long step under observation")
    task_svc.update(t.id, status="in_progress", workflow_step="write_failing_tests")
    task_svc.link_session(t.id, session_id)
    _old_transition(task_svc, t.id, seconds_ago=600)
    return t


def test_module_has_the_harness_side_observer_surface():
    """AC-2: the observer is real, importable server-side code -- not
    prompt text a step agent must remember to comply with."""
    from prism_service.services import drive_activity_observer as obs

    assert callable(obs.observe_tick)
    assert callable(obs.start_drive_activity_observer)
    assert callable(obs.is_enabled)
    assert isinstance(obs.DEFAULT_INTERVAL_S, int) and obs.DEFAULT_INTERVAL_S > 0


def test_tick_beats_only_on_observed_growth_not_on_elapsed_time(tmp_path):
    """AC-3/AC-4: repeated ticks reporting the SAME token count (no
    observed growth -- what a wedged/looping agent's transcript looks
    like) beat NOTHING, even across multiple ticks and even though real
    wall-clock time elapsed between them."""
    from prism_service.services import drive_activity_observer as obs
    from prism_service.services import drive_heartbeat

    cond, task_svc, scores_db = _cond(tmp_path)
    t = _driving_task(task_svc)

    # First-ever observation: growth cannot be judged from one reading,
    # so tick 1 must never beat even though the count is > 0.
    beaten1 = obs.observe_tick(task_svc, cond, scores_db,
                                tokens_reader=lambda *a, **k: 500)
    assert beaten1 == [], beaten1
    assert drive_heartbeat.heartbeat_age_s(scores_db, t.id) is None

    # Second/third tick: STILL 500 (no growth) -- a wedged/looping agent
    # whose transcript stopped advancing. Must still beat nothing.
    for _ in range(2):
        beaten = obs.observe_tick(task_svc, cond, scores_db,
                                   tokens_reader=lambda *a, **k: 500)
        assert beaten == [], beaten
    assert drive_heartbeat.heartbeat_age_s(scores_db, t.id) is None


def test_tick_beats_when_transcript_tokens_grow(tmp_path):
    """AC-1/AC-3, positive case: a token count that GROWS tick over tick
    (real assistant-turn output landing in the transcript) IS beaten, and
    the resulting heartbeat reads fresh."""
    from prism_service.services import drive_activity_observer as obs
    from prism_service.services import drive_heartbeat

    cond, task_svc, scores_db = _cond(tmp_path)
    t = _driving_task(task_svc)

    readings = iter([100, 340])
    obs.observe_tick(task_svc, cond, scores_db,
                      tokens_reader=lambda *a, **k: next(readings))
    beaten = obs.observe_tick(task_svc, cond, scores_db,
                               tokens_reader=lambda *a, **k: next(readings))
    assert beaten == [t.id], beaten

    age = drive_heartbeat.heartbeat_age_s(scores_db, t.id)
    assert age is not None and age < 5, age


def test_sustained_observed_growth_keeps_real_activity_for_driving(tmp_path):
    """AC-1, the acceptance criterion itself: a step past task_motion_s>120
    AND session_quiet_s>90, with CONTINUOUS observed transcript growth
    every tick, must read 'driving' on the REAL ConductorService.activity_for
    at every checkpoint after the first observation -- never adrift, never
    stalled."""
    from prism_service.services import drive_activity_observer as obs
    from prism_service.services import drive_heartbeat

    cond, task_svc, scores_db = _cond(tmp_path)
    t = _driving_task(task_svc)

    growing = iter(range(0, 100000, 50))
    states = []
    for _ in range(6):
        obs.observe_tick(task_svc, cond, scores_db,
                          tokens_reader=lambda *a, **k: next(growing))
        act = cond.activity_for(task_svc.get(t.id), {"session_quiet_s": 300})
        states.append(act["state"])

    # Tick 1 never beats (first observation, AC-4); from tick 2 onward,
    # observed growth has landed a beat every tick.
    assert states[0] != "driving", states
    assert states[1:] == ["driving"] * 5, states

    age = drive_heartbeat.heartbeat_age_s(scores_db, t.id)
    assert age is not None and age < drive_heartbeat.HEARTBEAT_WINDOW_S, age


def test_no_linked_session_never_beats(tmp_path):
    """AC-4: a task with no linked session at all (nothing to observe) is
    correctly excluded -- observe_tick never fabricates a beat for it."""
    from prism_service.services import drive_activity_observer as obs
    from prism_service.services import drive_heartbeat

    cond, task_svc, scores_db = _cond(tmp_path)
    t = task_svc.create(title="no session linked")
    task_svc.update(t.id, status="in_progress", workflow_step="write_failing_tests")
    _old_transition(task_svc, t.id, seconds_ago=600)

    for tokens in (100, 500, 900):
        beaten = obs.observe_tick(task_svc, cond, scores_db,
                                   tokens_reader=lambda *a, **k: tokens)
        assert beaten == [], beaten

    assert drive_heartbeat.heartbeat_age_s(scores_db, t.id) is None


def test_task_parked_at_a_gate_is_never_beaten(tmp_path):
    """AC-4: a gate-parked task is excluded from observation entirely --
    gate liveness belongs to the distinct adjudicator seat, and beating it
    here would misrepresent a WAITING task as driving."""
    from prism_service.services import drive_activity_observer as obs
    from prism_service.services import drive_heartbeat

    cond, task_svc, scores_db = _cond(tmp_path)
    t = task_svc.create(title="parked at green_gate")
    task_svc.update(t.id, status="in_progress", workflow_step="green_gate",
                     gate_state="pending")
    task_svc.link_session(t.id, "sess-gate")
    _old_transition(task_svc, t.id, seconds_ago=600)

    readings = iter([10, 10000])
    for _ in range(2):
        beaten = obs.observe_tick(task_svc, cond, scores_db,
                                   tokens_reader=lambda *a, **k: next(readings))
        assert beaten == [], beaten

    assert drive_heartbeat.heartbeat_age_s(scores_db, t.id) is None
