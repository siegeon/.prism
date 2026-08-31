"""A refused plan or story rubric rewinds instead of parking (task fb997b1d).

THE DEADLOCK. A task whose plan or story rubric REFUSES cannot be moved by
anything in the system:
  - `task_runner.eligible_task` skips it, because its current step is a gate
    (`task_runner.py:544` -- `if step is None or step["type"] == "gate"`).
  - `gate_adjudicator` withholds it rather than approving, because a
    deterministic tooth refused (`gate_adjudicator.py:221-228`).
  - `dispatch.after_step` will not drive a gate step, because a gate belongs
    to a distinct seat.
So only a person hand-editing plan_doc frees it. Measured live 2026-08-31:
tasks a928f3d5 and 02264017 were both wedged exactly this way.

TWO REWINDS ALREADY EXIST AND NEITHER COVERS THIS.
`green_rewind.maybe_rewind` fires only on a FAILED EvidenceReceipt at
green_gate. `ConductorService._auto_rewind` (7.13.133) fires only from
`gate_decide`, i.e. only when an actor explicitly REJECTS. The gap is the
case where NOBODY rejects: the rubric refuses, the adjudicator withholds,
and the row simply stops.

Pins the new non-policy module `services/plan_rewind.py`. The refusal source
DIFFERS per gate, and getting that wrong ships the story half as dead code --
see `test_the_story_gate_does_not_read_the_plan_teeth`.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

PLAN_REFUSAL = ("plan_checks: no acceptance criterion is shown to FAIL at "
                "the base commit -- 8 AC(s) carry an oracle")
STORY_REFUSAL = "story_complete: AC-3 has no oracle: marker"


def _setup(tmp_path, monkeypatch, *, step="plan_gate", gate_state="pending",
           plan_refusal=PLAN_REFUSAL, story_verified=False,
           story_reason=STORY_REFUSAL, budget=None):
    """A task parked on `step`, with each gate's refusal source stubbed.

    The two sources are deliberately INDEPENDENT, so a test can prove the
    module reads the right one for the gate it is standing on.
    """
    from prism_service.services import plan_gate_checks
    from prism_service.services import plan_rewind  # NEW module (red)
    from prism_service.services.task_service import TaskService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="rewind the refused rubric")
    task_svc.update(t.id, workflow_step=step, gate_state=gate_state)

    src = tmp_path / "src"
    (src / ".prism" / "behaviors").mkdir(parents=True)
    if budget is not None:
        (src / ".prism" / "behaviors" / "conductor.json").write_text(
            json.dumps({"rewind_budget": budget}), encoding="utf-8")
    monkeypatch.setattr(plan_rewind, "_source_path",
                        lambda project: str(src), raising=False)
    monkeypatch.setattr(plan_gate_checks, "refusal",
                        lambda task, project=None, **kw: plan_refusal)

    conductor_svc = types.SimpleNamespace(
        _validation_for_gate=lambda step_id: (
            "story_complete" if step_id == "story_gate" else "plan_coverage"),
        _verify_rubric_gate=lambda task, validation: {
            "verified": bool(story_verified),
            "reason": "" if story_verified else story_reason},
    )
    ctx = types.SimpleNamespace(task_svc=task_svc,
                                conductor_svc=conductor_svc,
                                project="testproj")
    return ctx, task_svc.get(t.id), task_svc


# ----------------------------------------------------------------------
# The refusal SOURCE differs per gate -- the review's material finding
# ----------------------------------------------------------------------

def test_the_story_gate_does_not_read_the_plan_teeth(tmp_path, monkeypatch):
    """The gap that would have shipped the story half as DEAD CODE.

    `plan_gate_checks.refusal` reads plan-phase content (plan_doc, verify,
    stop_if against a base ref), so against a story it returns "" almost
    always -- and an empty refusal means "nothing to rewind for". A module
    that asked the plan teeth about a story would simply never fire.

    Here the plan teeth say nothing is wrong while the story rubric refuses.
    The story gate must still rewind, which is only possible if it read the
    story rubric.
    """
    from prism_service.services import plan_rewind

    ctx, task, task_svc = _setup(tmp_path, monkeypatch, step="story_gate",
                                 plan_refusal="")
    out = plan_rewind.maybe_rewind(ctx, task, "testproj")

    assert out and out.get("ok") is True, (
        "a story_gate must read the story rubric, never the plan teeth")
    assert task_svc.get(task.id).workflow_step == "draft_story"


def test_the_plan_gate_does_not_read_the_story_rubric(tmp_path, monkeypatch):
    """The mirror: the plan teeth are the plan gate's source.

    The story rubric PASSES here while the plan teeth refuse. Only a module
    reading the plan teeth for a plan_gate rewinds.
    """
    from prism_service.services import plan_rewind

    ctx, task, task_svc = _setup(tmp_path, monkeypatch, step="plan_gate",
                                 story_verified=True)
    out = plan_rewind.maybe_rewind(ctx, task, "testproj")

    assert out and out.get("ok") is True
    assert task_svc.get(task.id).workflow_step == "verify_plan"

# ----------------------------------------------------------------------
# AC-1 / AC-2: the move itself, and the RIGHT source per gate
# ----------------------------------------------------------------------

def test_a_refused_plan_gate_rewinds_to_verify_plan(tmp_path, monkeypatch):
    """AC-1: a PENDING plan_gate whose plan rubric refused moves to
    verify_plan, the agent step the drive seat can reach."""
    from prism_service.services import plan_rewind

    ctx, task, task_svc, _ = _setup(tmp_path, monkeypatch, step="plan_gate")
    out = plan_rewind.maybe_rewind(ctx, _svc(), task, "testproj")

    assert out is not None and out["ok"] is True
    assert out["from_step"] == "plan_gate"
    assert out["to_step"] == "verify_plan"
    assert task_svc.get(task.id).workflow_step == "verify_plan"

def test_a_refused_story_gate_rewinds_to_draft_story(tmp_path, monkeypatch):
    """AC-2: a PENDING story_gate whose story rubric refused moves to
    draft_story -- and the PLAN teeth are never consulted for a story. Their
    three checks all read plan-phase content, so calling them here would
    return "" and ship the story mirror as dead code."""
    from prism_service.services import plan_rewind

    ctx, task, task_svc, calls = _setup(tmp_path, monkeypatch,
                                        step="story_gate")
    out = plan_rewind.maybe_rewind(ctx, _svc(), task, "testproj")

    assert out is not None and out["ok"] is True
    assert out["to_step"] == "draft_story"
    assert task_svc.get(task.id).workflow_step == "draft_story"
    assert calls == [], (
        "plan_gate_checks.refusal() must never be called for story_gate")


# ----------------------------------------------------------------------
# AC-3 / AC-4 / AC-5: what the move leaves behind
# ----------------------------------------------------------------------

def test_the_rewind_names_the_clause_that_refused(tmp_path, monkeypatch):
    """AC-3: gate_reason names WHICH clause refused and which attempt this
    is out of the budget, so the next drive acts on it without re-deriving
    the refusal."""
    from prism_service.services import plan_rewind

    ctx, task, task_svc, _ = _setup(tmp_path, monkeypatch, step="plan_gate",
                                    budget=3)
    plan_rewind.maybe_rewind(ctx, _svc(), task, "testproj")

    reason = task_svc.get(task.id).gate_reason
    assert PLAN_RUBRIC in reason
    assert "1/3" in reason

def test_the_rewind_leaves_no_open_gate_on_an_agent_step(tmp_path,
                                                         monkeypatch):
    """AC-4: the rewind writes gate_state="none", never "pending". An agent
    step carrying an open gate is invisible to is_open_gate_step(), the
    blind spot green_rewind.py:129 records."""
    from prism_service.services import plan_rewind
    from prism_service.services.task_service import is_open_gate_step

    ctx, task, task_svc, _ = _setup(tmp_path, monkeypatch, step="plan_gate")
    plan_rewind.maybe_rewind(ctx, _svc(), task, "testproj")

    moved = task_svc.get(task.id)
    assert moved.gate_state == "none"
    assert is_open_gate_step(moved.workflow_step, moved.gate_state) is False

def test_each_rewind_writes_one_audited_history_row(tmp_path, monkeypatch):
    """AC-5: exactly ONE history row per rewind, action="rewind", actor
    "conductor-adjudicator", naming the from-step and the to-step."""
    from prism_service.services import plan_rewind

    ctx, task, task_svc, _ = _setup(tmp_path, monkeypatch, step="plan_gate")
    plan_rewind.maybe_rewind(ctx, _svc(), task, "testproj")

    rows = [h for h in task_svc.history(task.id) if h.action == "rewind"]
    assert len(rows) == 1
    assert rows[0].actor == "conductor-adjudicator"
    assert "plan_gate" in rows[0].details
    assert "verify_plan" in rows[0].details


# ----------------------------------------------------------------------
# AC-6 / AC-7 / AC-8: the things it must REFUSE to do
# ----------------------------------------------------------------------

def test_the_budget_caps_the_loop(tmp_path, monkeypatch):
    """AC-6: past the budget the task PARKS with a reason naming the spent
    budget, and moves no row. A GREEN rewind never spends the plan budget --
    the counter reads only rows whose details name this gate."""
    from prism_service.services import plan_rewind

    ctx, task, task_svc, _ = _setup(tmp_path, monkeypatch, step="plan_gate",
                                    budget=2)
    task_svc.record_history(task.id, action="rewind",
                            details="green_gate -> implement_tasks; attempt=1",
                            actor="conductor-adjudicator")
    assert plan_rewind.maybe_rewind(ctx, _svc(), task, "testproj")["ok"] is True
    task = task_svc.get(task.id)
    task_svc.update(task.id, workflow_step="plan_gate", gate_state="pending")
    task = task_svc.get(task.id)
    assert plan_rewind.maybe_rewind(ctx, _svc(), task, "testproj")["ok"] is True
    task_svc.update(task.id, workflow_step="plan_gate", gate_state="pending")
    task = task_svc.get(task.id)

    out = plan_rewind.maybe_rewind(ctx, _svc(), task, "testproj")
    assert out["ok"] is False and out["parked"] is True
    assert "2" in out["reason"] and "budget" in out["reason"].lower()
    assert task_svc.get(task.id).workflow_step == "plan_gate"
    assert len([h for h in task_svc.history(task.id)
                if h.action == "rewind"]) == 3

def test_a_decided_gate_is_never_rewound(tmp_path, monkeypatch, decided):
    """AC-7: only gate_state="pending" is eligible. A decision an actor
    already made must never be undone."""
    from prism_service.services import plan_rewind

    ctx, task, task_svc, _ = _setup(tmp_path, monkeypatch, step="plan_gate",
                                    gate_state=decided)
    assert plan_rewind.maybe_rewind(ctx, _svc(), task, "testproj") is None
    assert task_svc.get(task.id).workflow_step == "plan_gate"

def test_an_inconclusive_verdict_does_not_rewind(tmp_path, monkeypatch):
    """AC-8: a scorer that COULD NOT JUDGE is not a refusal. It keeps a
    named escalation. `_verify_rubric_gate` answers verified=False for a
    scoring EXCEPTION too, and reading that as a failure is exactly the
    defect 7.13.190 records against green_rewind."""
    from prism_service.services import plan_rewind

    ctx, task, task_svc, _ = _setup(tmp_path, monkeypatch, step="plan_gate",
                                    teeth="")
    out = plan_rewind.maybe_rewind(
        ctx, _svc(plan_verified=False, plan_reason=RAISED), task, "testproj")
    assert out["ok"] is False and out["inconclusive"] is True
    assert task_svc.get(task.id).workflow_step == "plan_gate"
    assert str(out["reason"]).strip()
    assert str(out["reason"]) in task_svc.get(task.id).gate_reason

    # Both sources silent is "nothing to rewind for", never "rewind".
    quiet = plan_rewind.maybe_rewind(
        ctx, _svc(plan_verified=True, plan_reason=""), task, "testproj")
    assert quiet["ok"] is False and quiet["inconclusive"] is True
    assert task_svc.get(task.id).workflow_step == "plan_gate"

def test_a_step_that_is_not_a_rubric_gate_is_ignored(tmp_path, monkeypatch):
    """green_gate has its own rewind; this module must not touch it."""
    from prism_service.services import plan_rewind

    ctx, task, task_svc = _setup(tmp_path, monkeypatch, step="green_gate")

    assert plan_rewind.maybe_rewind(ctx, task, "testproj") is None
    assert task_svc.get(task.id).workflow_step == "green_gate"


# ----------------------------------------------------------------------
# AC-9 / AC-10 / AC-11: the point of the slice, and the wiring
# ----------------------------------------------------------------------

def test_the_drive_seat_can_reach_the_task_again(tmp_path, monkeypatch):
    """AC-9: the outcome the whole slice exists for. eligible_task returns
    None while the task sits at plan_gate (task_runner.py:544 skips a gate),
    and returns the id once the rewind lands it on verify_plan."""
    from prism_service.services import plan_rewind
    from prism_service.services import task_runner
    from prism_service import project_context

    ctx, task, task_svc, _ = _setup(tmp_path, monkeypatch, step="plan_gate")
    monkeypatch.setattr(project_context, "get_project", lambda p: ctx)
    monkeypatch.setattr(task_runner, "_spend_ceiling_crossed", lambda: False)
    monkeypatch.setattr(task_runner, "_system_overloaded", lambda: False)
    monkeypatch.setattr(task_runner, "_foreign_driver_on", lambda p, t: "")

    assert task_runner.eligible_task("testproj") is None
    plan_rewind.maybe_rewind(ctx, _svc(), task, "testproj")
    assert task_runner.eligible_task("testproj") == task.id

def test_the_rewind_makes_no_model_call(tmp_path, monkeypatch):
    """AC-10: the node is codified. It reads two deterministic scorers and
    reaches no model."""
    src = _MODULE.read_text(encoding="utf-8")
    assert "claude_cli" not in src
    assert "inference" not in src

def test_the_adjudicator_calls_the_rewind(tmp_path, monkeypatch):
    """AC-11: the call site exists in production code, beside
    green_rewind.maybe_rewind. A helper nobody calls is this project's
    recurring defect."""
    adj = (_SERVICE_ROOT / "prism_service" / "services"
           / "gate_adjudicator.py").read_text(encoding="utf-8")
    assert "plan_rewind" in adj
    assert "plan_rewind.maybe_rewind(" in adj

def test_the_module_imports_no_policy_module():
    """The slice REACTS to the rubric; it must never BE the rubric.

    A task whose allowed_files touches a POLICY_FILE fails its own gates on
    the candidate-controls-judge tooth, and a module importing one to mutate
    it is the same hazard one level down.
    """
    import inspect

    from prism_service.services import control_plane, plan_rewind

    src = inspect.getsource(plan_rewind)
    for policy in control_plane.POLICY_FILES:
        stem = Path(policy).stem
        if stem in ("control_plane", "conductor_service"):
            continue  # read-only access is via the injected ctx, not import
        assert f"import {stem}" not in src, (
            f"plan_rewind must not import the policy module {stem}")
