"""A machine seat must not re-approve a plan a human just rejected unchanged.

Task c2e6edaf, observed live on task d0b392b3 (2026-09-12): a distinct
reviewer rejected plan_gate at 18:09:25 with six source-checked findings
(invented AC ids, files that do not exist). plan_doc did not change. At
18:11:36 the conductor-adjudicator's certainty seat
(design_packet.adjudicate_root_plan_gate) approved the SAME plan_doc text,
because task.oracle (an unrelated field) gained backtick paths and pushed
the certainty score from 0.88 to 1.00. Neither machine seat ever asked "did
a human just refuse this exact plan?"

THE FIX: a fourth deterministic plan tooth, plan_gate_checks.
manual_reject_stands, reads the latest HUMAN gate_decide reject of
plan_gate from task history and compares the plan_doc content hash stamped
on that reject row (conductor_service.py's _reject_gate now stamps it)
against the CURRENT plan_doc. While they match, every machine seat that
consults plan_gate_checks.refusal() — conductor_flow's entry-time
autoclear, gate_adjudicator's re-sweep, and (because both check refusal()
BEFORE calling it) the certainty seat itself — declines, and the parked
gate_reason names who rejected it, when, and the first finding. The moment
plan_doc changes, the hash no longer matches and the seat may decide again.

  AC-1  plan_gate_checks.manual_reject_stands refuses while the latest
        human reject's stamped plan_doc hash matches the current plan_doc.
  AC-2  it stops refusing the instant plan_doc changes.
  AC-3  a machine actor's own decline (never a real "reject" today, but
        checked defensively) is not counted as "a human said no".
  AC-4  the entry-time autoclear seat withholds a ROOT task's plan_gate
        while the reject stands, and NEVER reaches the certainty seat
        (design_packet.adjudicate_root_plan_gate) at all — the literal
        d0b392b3 defect, reproduced and closed.
  AC-5  the adjudicator's own decline-reason surfacer names the same
        refusal, so the re-sweep seat and the entry seat cannot disagree.
  AC-6  GET /api/conductor/gate/readiness declines and names the reject
        while it stands, ahead of the design-packet approval-status
        branch it used to answer with instead.
  AC-7  the node .prism/behaviors/conductor/plan-gate-check.json declares
        the new check as a real codified step (not a bare Python branch).

RED AT BASE: plan_gate_checks.manual_reject_stands does not exist, and
conductor_service.py's _reject_gate stamps no plan_doc hash onto a plan_gate
reject row -- every test below fails at the base commit.
"""
from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_REPO_ROOT = _HERE.parents[4]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import plan_gate_checks as pgc  # noqa: E402

_RICH_PLAN = "\n".join([
    "## Summary",
    "A machine seat must not re-approve a plan a human just rejected "
    "unchanged, regardless of what an unrelated field's score does. No "
    "acceptance-criteria block here on purpose: this fixture is exercised "
    "with the plan_coverage rubric stubbed green, so it must not also trip "
    "the already_green_ac tooth's own AC-shaped parsing, which would refuse "
    "it for a reason that has nothing to do with the manual-reject tooth "
    "these tests pin.",
])


# ---------------------------------------------------------------------------
# AC-1..AC-3 -- the pure function, over a SYNTHETIC history (no services).
# ---------------------------------------------------------------------------

def _row(action="gate_decide", details="", actor="reviewer-42",
        timestamp="2026-09-12T18:09:25+00:00"):
    return SimpleNamespace(action=action, details=details, actor=actor,
                           timestamp=timestamp)


def _reject_row(plan_doc, actor="reviewer-42",
                timestamp="2026-09-12T18:09:25+00:00",
                reason="AC-123/AC-456/AC-789 do not exist"):
    h = pgc._plan_doc_sha256(plan_doc)
    return _row(details=(f"gate=plan_gate; action=reject; reason={reason}; "
                         f"plan_doc_sha256={h}"),
               actor=actor, timestamp=timestamp)


def test_a_standing_manual_reject_is_refused():
    task = SimpleNamespace(id="t1", plan_doc=_RICH_PLAN)
    history = [_reject_row(_RICH_PLAN)]
    reason = pgc.manual_reject_stands(task, history=history)
    assert reason, "a reject against the CURRENT plan_doc must refuse"
    assert "reviewer-42" in reason, reason
    assert "AC-123" in reason, reason
    assert "2026-09-12T18:09:25" in reason, reason


def test_a_plan_doc_edit_clears_the_standing_reject():
    task = SimpleNamespace(id="t1", plan_doc=_RICH_PLAN + "\n\nRevised.")
    history = [_reject_row(_RICH_PLAN)]
    assert pgc.manual_reject_stands(task, history=history) == ""


def test_no_reject_in_history_is_not_refused():
    task = SimpleNamespace(id="t1", plan_doc=_RICH_PLAN)
    history = [_row(details="gate=plan_gate; action=approve; reason=fine")]
    assert pgc.manual_reject_stands(task, history=history) == ""


def test_a_machine_actors_own_decline_never_counts_as_a_human_reject():
    task = SimpleNamespace(id="t1", plan_doc=_RICH_PLAN)
    history = [_reject_row(_RICH_PLAN, actor="conductor-adjudicator")]
    assert pgc.manual_reject_stands(task, history=history) == ""


def test_an_older_reject_row_with_no_stamped_hash_degrades_to_pass():
    task = SimpleNamespace(id="t1", plan_doc=_RICH_PLAN)
    history = [_row(details="gate=plan_gate; action=reject; reason=old-format")]
    assert pgc.manual_reject_stands(task, history=history) == ""


def test_the_latest_reject_wins_over_an_earlier_one():
    task = SimpleNamespace(id="t1", plan_doc="second draft")
    history = [
        _reject_row("first draft", timestamp="2026-09-12T10:00:00+00:00",
                   reason="first finding"),
        _reject_row("second draft", timestamp="2026-09-12T18:09:25+00:00",
                   reason="second finding"),
    ]
    reason = pgc.manual_reject_stands(task, history=history)
    assert "second finding" in reason, reason
    assert "first finding" not in reason, reason


def test_manual_reject_stands_is_a_registered_check():
    assert "manual_reject_stands" in pgc.CHECKS
    entry = pgc.run_check("manual_reject_stands",
                          SimpleNamespace(id="", plan_doc=""))
    assert entry["ok"] is True, entry  # no id -> degrades to pass


# ---------------------------------------------------------------------------
# AC-4..AC-6 -- the real seats, over a REAL TaskService/ConductorService,
# reproducing task d0b392b3 end to end.
# ---------------------------------------------------------------------------

def _root_env():
    """A fresh, isolated project via the real project_context machinery
    (mirrors test_plan_gate_deterministic_checks.py's _child_task_at_plan_gate)
    so plan_gate_checks' internal get_project(project).task_svc.history()
    call resolves to the SAME TaskService the test drives."""
    from prism_service.project_context import get_project
    project = "plan-reject-" + uuid.uuid4().hex[:8]
    svc = get_project(project).conductor_svc
    task = svc._task_svc.create(title="plan gate reject probe",
                                oracle="`services/x.py` is the fix",
                                proof_type="test", tags=[], parent_id="",
                                workflow="implement")
    svc._task_svc.update(task.id, workflow_step="plan_gate",
                         gate_state="pending", plan_doc=_RICH_PLAN,
                         likely_misfire="a stale field score cannot buy an "
                                       "approval a human already refused")
    svc._verify_gate = lambda t, step_id, proof_type=None: {
        "verified": True, "reason": "stub rubric green", "verifier": None,
        "validation": "plan_coverage"}
    pgc.clear_cache()
    return project, svc, task.id


def test_a_root_reject_then_resubmit_unchanged_withholds_the_autoclear_seat(
        monkeypatch):
    """AC-4: the literal defect. certainty stubbed to the observed 1.00 so a
    regression could only pass by NEVER REACHING the certainty seat at all."""
    from prism_service.api import conductor_flow as cf
    from prism_service.services import design_packet as dp

    project, svc, task_id = _root_env()

    res = svc.gate_decide(task_id, "reject",
                          reason="AC-123/AC-456/AC-789 do not exist",
                          actor="reviewer-42", session_id="reviewer-42",
                          model="human")
    assert res.get("ok") is True, res

    # The next drive pass re-presents the SAME text at plan_gate — the exact
    # d0b392b3 shape (a fresh round that forgot to revise plan_doc).
    svc._task_svc.update(task_id, workflow_step="plan_gate",
                         gate_state="pending")
    pgc.clear_cache()
    task = svc._task_svc.get(task_id)
    assert task.plan_doc == _RICH_PLAN, "plan_doc must be UNCHANGED"

    def _must_not_run(*a, **k):
        raise AssertionError(
            "the certainty seat ran while a manual reject still stands")
    monkeypatch.setattr(dp, "adjudicate_root_plan_gate", _must_not_run)
    monkeypatch.setattr(dp, "plan_gate_certainty",
                        lambda *a, **k: {"score": 1.0,
                                        "signals": {"plan_completeness": 1.0,
                                                   "oracle_quality": 1.0,
                                                   "diagram_quality": 1.0,
                                                   "scope_alignment": 1.0},
                                        "reasons": []})

    out = cf._autoclear_machine_gate(svc, task_id)
    task = svc._task_svc.get(task_id)

    assert out is None, out
    assert task.gate_state == "pending", task.gate_state
    assert "reviewer-42" in (task.gate_reason or ""), task.gate_reason
    assert "AC-123" in (task.gate_reason or ""), task.gate_reason


def test_a_plan_doc_revision_after_reject_lets_the_seat_decide_again(
        monkeypatch):
    """AC-2 at the seat level: once the revision is real, the certainty seat
    is reached again (approve or park on its own merits, either is fine —
    only that the standing-reject tooth itself has stood down)."""
    from prism_service.api import conductor_flow as cf
    from prism_service.services import design_packet as dp

    project, svc, task_id = _root_env()
    svc.gate_decide(task_id, "reject",
                    reason="AC-123/AC-456/AC-789 do not exist",
                    actor="reviewer-42", session_id="reviewer-42",
                    model="human")
    svc._task_svc.update(
        task_id, workflow_step="plan_gate", gate_state="pending",
        plan_doc=_RICH_PLAN + "\n\nAC-1/AC-2 replace the invented ids.")
    pgc.clear_cache()

    reached = {"called": False}
    real_certainty = dp.adjudicate_root_plan_gate

    def _spy(*a, **k):
        reached["called"] = True
        return real_certainty(*a, **k)
    monkeypatch.setattr(dp, "adjudicate_root_plan_gate", _spy)

    cf._autoclear_machine_gate(svc, task_id)
    assert reached["called"], (
        "a revised plan_doc must reach the certainty seat again")


def test_the_adjudicator_seat_reports_the_same_standing_reject():
    """AC-5: the re-sweep seat's own decline-reason surfacer must not
    disagree with the entry-time seat about why this gate is parked."""
    from prism_service.services import gate_adjudicator as ga

    project, svc, task_id = _root_env()
    svc.gate_decide(task_id, "reject",
                    reason="AC-123/AC-456/AC-789 do not exist",
                    actor="reviewer-42", session_id="reviewer-42",
                    model="human")
    svc._task_svc.update(task_id, workflow_step="plan_gate",
                         gate_state="pending")
    pgc.clear_cache()
    svc._validation_for_gate = lambda *a, **k: "plan_coverage"
    svc._verify_rubric_gate = lambda *a, **k: {"verified": True, "reason": ""}
    task = svc._task_svc.get(task_id)

    reason = ga._pending_decline_reason(svc, task, "plan_gate", project)
    assert "reviewer-42" in reason, reason
    assert "AC-123" in reason, reason


def test_readiness_declines_and_names_the_reject_while_it_stands():
    """AC-6: GET /api/conductor/gate/readiness, not just the seats."""
    from prism_service.api import conductor as capi

    project, svc, task_id = _root_env()
    svc.gate_decide(task_id, "reject",
                    reason="AC-123/AC-456/AC-789 do not exist",
                    actor="reviewer-42", session_id="reviewer-42",
                    model="human")
    svc._task_svc.update(task_id, workflow_step="plan_gate",
                         gate_state="pending")
    pgc.clear_cache()

    import prism_service.api.conductor as capi_mod
    orig_svc = capi_mod._svc
    capi_mod._svc = lambda p: svc
    try:
        out = capi.gate_readiness(task_id=task_id, project=project)
    finally:
        capi_mod._svc = orig_svc

    assert out["receipt_ok"] is False, out
    assert "reviewer-42" in out["receipt_refusal"], out
    assert "AC-123" in out["receipt_refusal"], out


# ---------------------------------------------------------------------------
# AC-7 -- the node file, not a bare Python branch.
# ---------------------------------------------------------------------------

def test_the_node_file_declares_the_new_check():
    import json
    node_path = (_REPO_ROOT / ".prism" / "behaviors" / "conductor"
                / "plan-gate-check.json")
    node = json.loads(node_path.read_text(encoding="utf-8"))
    ids = [s["id"] for s in node["steps"]]
    assert "manual_reject_stands" in ids, ids
    step = next(s for s in node["steps"] if s["id"] == "manual_reject_stands")
    assert step["kind"] == "http-callback"
    assert "plan-gate-check-one" in step["url"]
    assert re.search(r'"check":\s*"manual_reject_stands"', step["body"])
