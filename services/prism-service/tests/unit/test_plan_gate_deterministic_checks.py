"""plan_gate stops advancing on the defects a human used to catch by hand.

Task 72ccaf94 needed FIVE rounds at plan_gate. Three of its recurring
defects are mechanically checkable, and each one below is written against
the REAL text that was rejected:

  1. the plan said a test "does not exist" while it sat at
     services/prism-service/tests/unit/test_sqlite_maint.py:34;
  2. a test named in task.stop_if was absent from task.verify;
  3. an AC that already passes at the base commit was offered as the
     observation for an oracle clause (AC-5, then AC-12, then AC-13).

The seat test at the bottom is the one that matters: a machine seat must
WITHHOLD the gate on a refusal, not merely report it.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_REPO_ROOT = _HERE.parents[4]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import plan_gate_checks as pgc  # noqa: E402


# ----------------------------------------------------------------------
# 1. absent_file_claim
# ----------------------------------------------------------------------
def test_a_false_absence_claim_about_a_real_file_is_refused():
    """The literal round-1 defect: the file was there the whole time."""
    plan = ("No coverage exists today: tests/unit/test_sqlite_maint.py "
            "does not exist, so nothing pins checkpoint_db.")
    reason = pgc.absent_file_claim(plan, _REPO_ROOT)
    assert "test_sqlite_maint.py" in reason, reason
    assert "services/prism-service/tests/unit/test_sqlite_maint.py" in reason


def test_a_plan_that_proposes_to_create_the_file_is_not_refused():
    """A plan saying a file is absent AND that it writes it is the normal,
    correct shape -- the tooth must never fire on it."""
    plan = ("tests/unit/test_sqlite_maint.py does not exist yet, so this "
            "slice creates it with the failing case.")
    assert pgc.absent_file_claim(plan, _REPO_ROOT) == ""


def test_an_absence_claim_about_a_path_that_really_is_absent_passes():
    plan = "tests/unit/test_no_such_module_anywhere_xyz.py does not exist."
    assert pgc.absent_file_claim(plan, _REPO_ROOT) == ""


def test_no_repo_to_resolve_against_degrades_to_a_pass():
    plan = "tests/unit/test_sqlite_maint.py does not exist."
    assert pgc.absent_file_claim(plan, None) == ""


# ----------------------------------------------------------------------
# 2. stop_if_pinned
# ----------------------------------------------------------------------
def test_a_stop_if_test_missing_from_verify_is_refused():
    reason = pgc.stop_if_pinned(
        ["The WAL grows past the cap: tests/unit/test_sqlite_maint.py"],
        ["services/prism-service/tests/unit/test_brain_wal_bounded.py"])
    assert "test_sqlite_maint" in reason, reason


def test_a_stop_if_test_pinned_by_verify_passes():
    assert pgc.stop_if_pinned(
        ["The WAL grows past the cap: tests/unit/test_sqlite_maint.py"],
        ["services/prism-service/tests/unit/test_sqlite_maint.py"]) == ""


def test_a_stop_if_clause_that_names_no_test_is_accepted():
    assert pgc.stop_if_pinned(
        ["The daemon takes longer than 900 s to finish a pass."], []) == ""


def test_a_bare_test_function_name_in_stop_if_counts():
    reason = pgc.stop_if_pinned(["test_wal_stays_bounded must stay red"], [])
    assert "test_wal_stays_bounded" in reason, reason


# ----------------------------------------------------------------------
# 3. already_green_ac
# ----------------------------------------------------------------------
_ALL_GUARDS = ("## Acceptance Criteria\n"
               "- AC-5 - the existing WAL bound still holds after the change.\n"
               "  - oracle: pytest tests/unit/test_brain_wal_bounded.py passes.\n"
               "- AC-10 - the existing suite stays green, unedited.\n"
               "  - oracle: pytest tests/unit/test_sqlite_maint.py\n")
_ONE_RED = ("## Acceptance Criteria\n"
            "- AC-1 - the orphan is gone. RED at HEAD.\n"
            "  - oracle: pytest tests/unit/test_brain_fts_no_orphans.py\n"
            "- AC-10 - the existing suite stays green, unedited.\n"
            "  - oracle: pytest tests/unit/test_sqlite_maint.py\n")


def test_a_plan_whose_every_ac_is_already_true_is_refused():
    """Rounds 2 and 3 both shipped this shape: every criterion green before
    the fix, so not one of them observes it."""
    reason = pgc.already_green_ac(_ALL_GUARDS, None, "", measure=False)
    assert "FAIL at the base commit" in reason, reason


def test_one_red_criterion_is_enough_and_guards_ride_along():
    """Round 5, the revision the human approved: most ACs declare RED at
    HEAD and the ship-hygiene ones stay green beside them."""
    assert pgc.already_green_ac(_ONE_RED, None, "", measure=False) == ""


def test_an_ac_that_passes_at_the_base_commit_is_refused():
    calls = []

    def runner(root, rev, targets, timeout_s):
        calls.append((rev, tuple(targets)))
        return 0                      # green at base -- observes nothing

    reason = pgc.already_green_ac(_ONE_RED, _REPO_ROOT, "deadbeef",
                                  measure=True, runner=runner)
    assert calls, "the base commit was never measured"
    assert "AC-1" in reason and "deadbeef" in reason, reason


def test_an_ac_that_fails_at_the_base_commit_passes():
    assert pgc.already_green_ac(
        _ONE_RED, _REPO_ROOT, "deadbeef", measure=True,
        runner=lambda *a, **k: 1) == ""


def test_an_unmeasurable_base_commit_degrades_to_a_pass():
    """Never refuse a plan because the checker could not measure."""
    assert pgc.already_green_ac(
        _ONE_RED, _REPO_ROOT, "deadbeef", measure=True,
        runner=lambda *a, **k: None) == ""


def test_a_plan_with_no_acceptance_criteria_at_all_is_not_refused_here():
    """The plan_coverage rubric already owns "there are no ACs"; this tooth
    must not double-refuse for it."""
    assert pgc.already_green_ac("# Plan\n\nSome prose.\n", None, "",
                                measure=False) == ""


# The calibration itself, against the FOUR real plan revisions the human
# actually judged. Two were rejected, two were accepted; the tooth has to
# separate them or it is not measuring what a human measured.
_EVIDENCE = Path.home() / ".prism" / "evidence" / \
    "72ccaf94-78f8-4a6d-add4-b457776fe489"


def test_the_tooth_separates_the_rejected_plans_from_the_approved_one():
    import pytest
    if not _EVIDENCE.is_dir():
        pytest.skip(f"task 72ccaf94 evidence not on this machine: {_EVIDENCE}")
    verdicts = {}
    for name in ("plan-r2.md", "plan-r3.md", "plan-r4.md", "plan-r5.md"):
        f = _EVIDENCE / name
        if not f.is_file():
            pytest.skip(f"missing {f}")
        verdicts[name] = pgc.already_green_ac(
            f.read_text(encoding="utf-8", errors="replace"),
            _REPO_ROOT, "", measure=False)
    assert verdicts["plan-r2.md"], "round 2 was REJECTED and must not pass"
    assert verdicts["plan-r3.md"], "round 3 was REJECTED and must not pass"
    assert verdicts["plan-r4.md"] == "", verdicts["plan-r4.md"]
    assert verdicts["plan-r5.md"] == "", verdicts["plan-r5.md"]


# ----------------------------------------------------------------------
# The seat: a refusal WITHHOLDS the gate, it does not merely report
# ----------------------------------------------------------------------
_CLEAN_PLAN = ("## Acceptance Criteria\n"
               "- AC-1 - the cap holds. RED at HEAD.\n"
               "  - oracle: pytest tests/unit/test_sqlite_maint.py\n")


def _child_task_at_plan_gate(*, stop_if, verify, plan_doc):
    """A CHILD task parked at a pending plan_gate. A child is the shape the
    machine seat may clear on the rubric alone (owner 2026-08-27, task
    3c774abd), so it isolates the new teeth from the owner's own root stop.
    """
    from prism_service.project_context import get_project
    project = "pgc-" + uuid.uuid4().hex[:8]
    svc = get_project(project).conductor_svc
    parent = svc._task_svc.create(title="plan gate teeth parent")
    task = svc._task_svc.create(title="plan gate teeth probe")
    svc._task_svc.update(task.id, parent_id=parent.id, plan_doc=plan_doc,
                         stop_if=stop_if, verify=verify,
                         workflow_step="plan_gate", gate_state="pending")
    svc._verify_gate = lambda *a, **k: {"verified": True,
                                        "reason": "rubric stubbed green"}
    pgc.clear_cache()
    return project, svc, task.id


def test_the_autoclear_seat_withholds_plan_gate_on_a_refusal():
    from prism_service.api import conductor_flow as cf
    project, svc, task_id = _child_task_at_plan_gate(
        stop_if=["The WAL grows: tests/unit/test_sqlite_maint.py"],
        verify=["services/prism-service/tests/unit/test_other.py"],
        plan_doc=_CLEAN_PLAN)
    res = cf._autoclear_machine_gate(svc, task_id)
    t = svc._task_svc.get(task_id)
    assert res is None, res
    assert t.gate_state == "pending", t.gate_state
    assert "stop_if names" in (t.gate_reason or ""), t.gate_reason


def test_the_autoclear_seat_still_clears_a_clean_plan():
    from prism_service.api import conductor_flow as cf
    project, svc, task_id = _child_task_at_plan_gate(
        stop_if=["The WAL grows: tests/unit/test_sqlite_maint.py"],
        verify=["services/prism-service/tests/unit/test_sqlite_maint.py"],
        plan_doc=_CLEAN_PLAN)
    res = cf._autoclear_machine_gate(svc, task_id)
    t = svc._task_svc.get(task_id)
    assert res is not None, (res, t.gate_reason)
    assert t.gate_state != "pending", (t.gate_state, t.gate_reason)


def test_the_adjudicator_seat_reports_the_same_refusal():
    """Both seats must ask the same question -- a tooth added to one seat
    and not the other is how a stale card and a live gate disagree."""
    from prism_service.services import gate_adjudicator as ga
    project, svc, task_id = _child_task_at_plan_gate(
        stop_if=["The WAL grows: tests/unit/test_sqlite_maint.py"],
        verify=["services/prism-service/tests/unit/test_other.py"],
        plan_doc=_CLEAN_PLAN)
    svc._validation_for_gate = lambda *a, **k: "plan_coverage"
    svc._verify_rubric_gate = lambda *a, **k: {"verified": True, "reason": ""}
    task = svc._task_svc.get(task_id)
    reason = ga._pending_decline_reason(svc, task, "plan_gate", project)
    assert "stop_if names" in reason, reason
