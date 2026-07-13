"""RED scaffold — conductor per-step ETA that sharpens over time (task 68a9720e).

The conductor tile today only fills the CURRENT step's bar against one global
median (_median_step_s). It cannot answer "how long until this task is DONE".
This scaffold pins the LEARNED per-step ETA forward-projection + its UI surface:

  BACKEND (conductor_service.py):
    * _per_step_typical() -> ({step: median_dwell_s}, {step: n}). A step's dwell
      is the gap between the advance INTO that step and the NEXT advance,
      attributed to the to-step of the EARLIER advance_task row, learned across
      ALL tasks' history. (The same regex-on-details / median-per-bucket seam
      _median_step_s uses, but bucketed per to-step.)
    * _eta_s(current_step, in_step_s, global_typical) -> (eta_s, sample_n,
      total_s). Forward projection over WORKFLOW_STEPS:
        remaining = max(0, typ(cur) - in_step_s) + sum(typ(s) for remaining s)
      typ(step) = the per-step learned median ONLY when that step has >= 2
      samples, else the global median fallback. total_s = sum(typ over ALL
      steps). sample_n = the CURRENT step's sample count (confidence). Returns
      (None, 0, None) when current_step is not a WORKFLOW_STEPS id. eta clamps
      >= 0 and is never NaN/negative.
    * phase_progress() payload carries eta_s / eta_sample_n / eta_total_s
      (rounded; null when not in-flow) — read END-TO-END off the task's REAL
      workflow_step (not a passed-in arg), so a DEAD method can't false-green it.

  FRONTEND (UI seam guards — the field must be CONSUMED, not just defined):
    * SdlcProgress.tsx PhaseProgress type carries eta_s/eta_sample_n/eta_total_s
      and renders a '~Xm left' caption with a 'rough' qualifier when n<2.
    * ConductorPage.tsx renders an ETA chip + a draining EtaCountdownBar.

ALL of these FAIL on feat/conductor-per-step-eta today (the port at c437414 is
NOT yet applied to this branch).
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.models.workflow import WORKFLOW_STEPS  # noqa: E402

_STEP_IDS = [s["id"] for s in WORKFLOW_STEPS]
_PID = "per_step_eta_pid"


def _conductor(tmp_path):
    """ConductorService + TaskService whose scores.db lives at
    <tmp>/projects/<pid>/scores.db (matches the token-source harness)."""
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    proj_dir = tmp_path / "projects" / _PID
    proj_dir.mkdir(parents=True, exist_ok=True)
    scores_db = str(proj_dir / "scores.db")
    task_svc = TaskService(str(proj_dir / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    return task_svc, cond


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _seed_advances(task_svc, task_id: str, base: float,
                   steps_and_gaps: list[tuple[str, float]]) -> None:
    """Insert advance_task history rows DIRECTLY (the real seam history() reads)
    with controlled timestamps. steps_and_gaps = [(to_step, secs_until_next)...]
    The dwell attributed to to_step is the gap to the NEXT advance row."""
    t = base
    for to_step, gap in steps_and_gaps:
        task_svc._db.execute(
            "INSERT INTO task_history (task_id, actor, action, details, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, "conductor", "advance_task",
             f"from=x; to={to_step}", _iso(t)),
        )
        t += gap
    task_svc._db.commit()


# ----------------------------------------------------------------------
# Oracle 1 — _per_step_typical attributes the inter-advance gap to the EARLIER
# to-step, learned across ALL tasks; returns ({step: median_s}, {step: n}).
# ----------------------------------------------------------------------

def test_per_step_typical_attributes_gap_to_earlier_to_step(tmp_path):
    task_svc, cond = _conductor(tmp_path)
    base = 1_700_000_000.0
    # Two completed tasks. draft_story dwell = gap to the NEXT advance.
    # task A: into draft_story, +100s -> verify_plan, +300s -> write_failing_tests
    a = task_svc.create(title="hist A")
    _seed_advances(task_svc, a.id, base, [
        ("draft_story", 100.0), ("verify_plan", 300.0), ("write_failing_tests", 0.0),
    ])
    # task B: into draft_story, +200s -> verify_plan, +500s -> write_failing_tests
    b = task_svc.create(title="hist B")
    _seed_advances(task_svc, b.id, base + 10_000, [
        ("draft_story", 200.0), ("verify_plan", 500.0), ("write_failing_tests", 0.0),
    ])

    assert hasattr(cond, "_per_step_typical"), (
        "ConductorService must expose _per_step_typical()"
    )
    per, counts = cond._per_step_typical()
    # draft_story dwells: 100, 200 -> median 150; n=2.
    assert counts.get("draft_story") == 2, counts
    assert per.get("draft_story") == pytest.approx(150.0), per
    # verify_plan dwells: 300, 500 -> median 400; n=2.
    assert counts.get("verify_plan") == 2, counts
    assert per.get("verify_plan") == pytest.approx(400.0), per


# ----------------------------------------------------------------------
# Oracle 2 — _eta_s forward-projects: a task at draft_story projects
# max(0, typ(draft_story)-in_step) + sum(typ(remaining steps)).
# ----------------------------------------------------------------------

def test_eta_s_projects_sum_of_remaining_step_medians_minus_in_step(tmp_path):
    task_svc, cond = _conductor(tmp_path)
    base = 1_700_000_000.0
    # Build >=2 samples for EVERY step so typ(step) == its learned per-step
    # median (not the global fallback) and the projection is fully determined.
    per_step_dwell = {sid: float((i + 1) * 60) for i, sid in enumerate(_STEP_IDS)}
    for k in range(2):  # two completed tasks -> n=2 per step
        tk = task_svc.create(title=f"full-walk {k}")
        seq = [(sid, per_step_dwell[sid]) for sid in _STEP_IDS] + [("done_sentinel", 0.0)]
        _seed_advances(task_svc, tk.id, base + k * 100_000, seq)

    per, counts = cond._per_step_typical()
    for sid in _STEP_IDS:
        assert counts.get(sid, 0) >= 2, (sid, counts)

    in_step = 25.0
    eta_s, sample_n, total_s = cond._eta_s("draft_story", in_step, 999.0)
    idx = _STEP_IDS.index("draft_story")
    expected_remaining = max(0.0, per_step_dwell["draft_story"] - in_step) + sum(
        per_step_dwell[s] for s in _STEP_IDS[idx + 1:]
    )
    assert eta_s == pytest.approx(expected_remaining), (eta_s, expected_remaining)
    # sample_n reflects the CURRENT step's confidence.
    assert sample_n == counts["draft_story"]
    # total_s is the full-SDLC budget = sum over ALL steps.
    assert total_s == pytest.approx(sum(per_step_dwell[s] for s in _STEP_IDS))


# ----------------------------------------------------------------------
# Oracle 3 — eta_sample_n equals the CURRENT step's sample count (confidence).
# ----------------------------------------------------------------------

def test_eta_sample_n_is_current_step_history_depth(tmp_path):
    task_svc, cond = _conductor(tmp_path)
    base = 1_700_000_000.0
    # Three completed tasks all dwelling in write_failing_tests -> n=3 there.
    for k in range(3):
        tk = task_svc.create(title=f"wft {k}")
        _seed_advances(task_svc, tk.id, base + k * 50_000, [
            ("write_failing_tests", 120.0), ("red_gate", 0.0),
        ])
    _, sample_n, _ = cond._eta_s("write_failing_tests", 10.0, 900.0)
    assert sample_n == 3, sample_n
    # A step with NO history -> sample_n 0.
    _, sample_n_empty, _ = cond._eta_s("implement_tasks", 10.0, 900.0)
    assert sample_n_empty == 0, sample_n_empty


# ----------------------------------------------------------------------
# Oracle 4 — a step with < 2 samples uses the GLOBAL median fallback (not the
# thin per-step median); >= 2 samples uses the learned per-step median.
# ----------------------------------------------------------------------

def test_under_two_samples_uses_global_fallback(tmp_path):
    task_svc, cond = _conductor(tmp_path)
    base = 1_700_000_000.0
    # draft_story gets exactly ONE sample (n=1) of 999s — must NOT be used.
    one = task_svc.create(title="single draft sample")
    _seed_advances(task_svc, one.id, base, [
        ("draft_story", 999.0), ("verify_plan", 0.0),
    ])
    per, counts = cond._per_step_typical()
    assert counts.get("draft_story", 0) == 1, counts

    GLOBAL = 42.0
    # Project from draft_story with in_step 0; every step (incl. draft_story)
    # has < 2 samples -> the whole projection is GLOBAL * (#steps from cur).
    eta_s, _, total_s = cond._eta_s("draft_story", 0.0, GLOBAL)
    idx = _STEP_IDS.index("draft_story")
    n_remaining_incl_cur = len(_STEP_IDS) - idx
    assert eta_s == pytest.approx(GLOBAL * n_remaining_incl_cur), eta_s
    assert total_s == pytest.approx(GLOBAL * len(_STEP_IDS)), total_s


# ----------------------------------------------------------------------
# Oracle 5 — eta clamps >= 0 / never NaN-negative; (None,0,None) off-flow.
# ----------------------------------------------------------------------

def test_eta_clamped_nonnegative_and_none_when_off_flow(tmp_path):
    task_svc, cond = _conductor(tmp_path)
    # in_step_s huge -> current-step remainder floors at 0, eta stays >= 0.
    eta_s, _, total_s = cond._eta_s("green_gate", 10_000_000.0, 60.0)
    assert eta_s is not None
    assert eta_s >= 0.0, eta_s
    assert not math.isnan(eta_s), eta_s
    # green_gate is the LAST step -> remaining-after is empty, eta == 0.
    assert eta_s == pytest.approx(0.0), eta_s
    # Off-flow current_step -> (None, 0, None).
    none_eta, none_n, none_total = cond._eta_s("not_a_real_step", 5.0, 60.0)
    assert none_eta is None and none_n == 0 and none_total is None


# ----------------------------------------------------------------------
# Oracle 6 (END-TO-END) — phase_progress payload carries eta_s/eta_sample_n/
# eta_total_s, read off the task's REAL workflow_step. This pins the SEAM:
# a dead _eta_s that's never wired into the payload fails here.
# ----------------------------------------------------------------------

def test_phase_progress_payload_carries_eta_fields_in_flow(tmp_path):
    task_svc, cond = _conductor(tmp_path)
    base = 1_700_000_000.0
    # Seed >=2 samples per step from completed tasks so eta is learned + > 0.
    per_step_dwell = {sid: 60.0 for sid in _STEP_IDS}
    for k in range(2):
        tk = task_svc.create(title=f"seed walk {k}")
        seq = [(sid, per_step_dwell[sid]) for sid in _STEP_IDS] + [("end", 0.0)]
        _seed_advances(task_svc, tk.id, base + k * 100_000, seq)

    # The DRIVEN task: real advance into draft_story (real workflow_step write).
    driven = task_svc.create(title="in-flight task")
    cond.advance_task(driven.id)          # -> review_previous_notes
    cond.advance_task(driven.id)          # -> draft_story
    task_svc.update(driven.id, status="in_progress")

    pp = cond.phase_progress(driven.id)
    assert "eta_s" in pp and "eta_sample_n" in pp and "eta_total_s" in pp, (
        f"phase_progress payload must carry the ETA fields; got keys {sorted(pp)}"
    )
    assert pp["eta_s"] is not None and pp["eta_s"] >= 0.0, pp["eta_s"]
    assert pp["eta_total_s"] is not None and pp["eta_total_s"] > 0.0, pp["eta_total_s"]
    assert isinstance(pp["eta_sample_n"], int), pp["eta_sample_n"]
    # Sharpens visibly: current step (draft_story) has n=2 from the seed walks.
    assert pp["eta_sample_n"] == 2, pp["eta_sample_n"]


def test_phase_progress_eta_null_when_not_in_flow(tmp_path):
    """A task that has NOT entered the workflow (workflow_step '') is off-flow:
    eta_s / eta_total_s are null."""
    task_svc, cond = _conductor(tmp_path)
    t = task_svc.create(title="never-advanced")  # workflow_step == ""
    pp = cond.phase_progress(t.id)
    assert pp.get("eta_s") is None, pp.get("eta_s")
    assert pp.get("eta_total_s") is None, pp.get("eta_total_s")


# ----------------------------------------------------------------------
# UI seam guards — the field must be CONSUMED in the SPA, not just on the wire.
# ----------------------------------------------------------------------

def _read_web(*parts: str) -> str:
    return (_SERVICE_ROOT / "prism_service" / "web" / "src" / Path(*parts)).read_text(
        encoding="utf-8"
    )


def test_sdlc_progress_type_and_caption_render_eta():
    tsx = _read_web("components", "conductor", "SdlcProgress.tsx")
    assert "eta_s" in tsx and "eta_sample_n" in tsx and "eta_total_s" in tsx, (
        "SdlcProgress.tsx PhaseProgress type must carry eta_s/eta_sample_n/eta_total_s"
    )
    assert "left" in tsx and "rough" in tsx, (
        "SdlcProgress.tsx caption must render '~Xm left' with a 'rough' qualifier "
        "when eta_sample_n < 2"
    )


def test_conductor_page_eta_ui_RETIRED_superseded_by_ring_arc_and_idle_clock():
    """RETIRED (inverted-flow #4). The AC was a task-card ETA UI on the conductor
    tile (an EtaCountdownBar + an '~Xm left' chip consuming eta_s/eta_total_s).
    The showcase ConductorPage that SUPERSEDES the PR-#212 tile (owner chose
    'timeline D') NEVER ported that ETA surface — so re-pointing this test to the
    ring arc + idle clock (as commit 89194d6 did) was laundering: it made an
    ETA-named test pass off unrelated elements. We RETIRE the ETA-UI assertion
    honestly here.

    The real, task-card ETA remains an OPEN, tracked deliverable on task
    68a9720e — it is deliberately NOT fake-satisfied by this test. What the
    backend DOES deliver (the learned per-step ETA projection + phase_progress
    payload) stays proven by the runtime oracles above in this file, and the
    SdlcProgress '~Xm left/rough' caption stays proven by
    test_sdlc_progress_type_and_caption_render_eta.
    """
    tsx = _read_web("pages", "ConductorPage.tsx")
    # The ETA UI is genuinely absent — assert the retirement, don't fake it. The
    # tile neither renders an EtaCountdownBar nor CONSUMES the ETA payload fields
    # (eta_total_s / eta_sample_n). (Note: the string 'eta_s' appears only in a
    # source COMMENT, so we do NOT key on it — that would be inverse-laundering.)
    assert "EtaCountdownBar" not in tsx, (
        "the tile must not render an EtaCountdownBar — the task-card ETA UI AC is "
        "retired here and still tracked on task 68a9720e; do not force it green"
    )
    assert "eta_total_s" not in tsx and "eta_sample_n" not in tsx, (
        "the tile does not consume the ETA payload fields — retirement premise"
    )
    # Pin the REAL progress/motion surfaces the showcase tile DOES render, under
    # an honest name (these are NOT the ETA — they are what superseded it).
    assert "strokeDashoffset" in tsx, (
        "the tile conveys progress via the draining completion-ring arc"
    )
    assert "fmtIdle" in tsx, (
        "the tile conveys motion via the honest idle clock (fmtIdle)"
    )
