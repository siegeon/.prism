"""A refused plan or story rubric rewinds instead of parking (task fb997b1d).

THE DEADLOCK. A task whose plan or story rubric REFUSES cannot be moved by
anything:
  - `task_runner.eligible_task` skips it, because its current step is a gate
    (`task_runner.py:544` -- `if step is None or step["type"] == "gate"`).
  - `gate_adjudicator` withholds it rather than approving, because a
    deterministic tooth refused (`gate_adjudicator.py:227-233`).
  - the handoff will not drive a gate step, because a gate belongs to a
    distinct seat.
So only a person hand-editing plan_doc frees it. Measured live on
2026-08-31: tasks a928f3d5 and 02264017 are both wedged this way.

TWO REWINDS ALREADY EXIST AND NEITHER COVERS THIS. `green_rewind.maybe_rewind`
fires only on a FAILED EvidenceReceipt at green_gate.
`ConductorService._auto_rewind` (7.13.133) fires only from `gate_decide`,
i.e. only when an actor explicitly REJECTS. The gap is the case where nobody
rejects: the rubric refuses, the adjudicator withholds, the row stops.

Pins the new non-policy module `prism_service/services/plan_rewind.py`. Every
test imports it INSIDE the test body, so at the base commit each one FAILS
with a ModuleNotFoundError trace naming the absent module (rc=1), rather than
erroring at collection.
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

_MODULE = _SERVICE_ROOT / "prism_service" / "services" / "plan_rewind.py"

PLAN_TEETH = ("already_green_ac: AC-4 is already green at the base commit "
              "d4f1a02, so the plan proves nothing")
PLAN_RUBRIC = "plan_coverage: 3 of 11 AC(s) carry no oracle: line"
STORY_RUBRIC = "story_complete: AC-3 has no oracle: marker"
RAISED = "rubric scoring raised: ValueError('no rubric on file')"
def _svc(*, plan_verified=False, plan_reason=PLAN_RUBRIC,
         story_verified=False, story_reason=STORY_RUBRIC):
    """A stand-in for ConductorService carrying ONLY the two members
    `plan_rewind` may read: the REAL step -> validation map, and the rubric
    scorer's verdict. The two verdicts are independent, so a test can prove
    the module reads the source that belongs to the gate it is on."""
    from prism_service.services.conductor_service import ConductorService

    verdicts = {
        "plan_coverage": {"verified": plan_verified, "reason": plan_reason},
        "story_complete": {"verified": story_verified, "reason": story_reason},
    }
    return types.SimpleNamespace(
        _validation_for_gate=ConductorService._validation_for_gate,
        _verify_rubric_gate=lambda task, validation: verdicts[validation],
    )


def _setup(tmp_path, monkeypatch, *, step="plan_gate", gate_state="pending",
           teeth=PLAN_TEETH, budget=None, status="in_progress"):
    """A task parked on `step`, with the plan teeth stubbed and the budget
    file under a scratch source root. Returns (ctx, task, task_svc, calls),
    where `calls` records every plan_gate_checks.refusal() call."""
    from prism_service.services.task_service import TaskService
    from prism_service.services import plan_gate_checks

    task_svc = TaskService(str(tmp_path / "tasks.db"), project="testproj")
    task = task_svc.create(title="rewind me")
    task_svc.update(task.id, workflow_step=step, gate_state=gate_state,
                    status=status)

    src = tmp_path / "src"
    (src / ".prism" / "behaviors").mkdir(parents=True)
    if budget is not None:
        (src / ".prism" / "behaviors" / "conductor.json").write_text(
            json.dumps({"rewind_budget": budget}), encoding="utf-8")
    monkeypatch.setenv("PRISM_SOURCE_PATH", str(src))

    calls: list = []

    def _refusal(task_arg, project="default", **kw):
        calls.append(project)
        return teeth

    monkeypatch.setattr(plan_gate_checks, "refusal", _refusal)
    ctx = types.SimpleNamespace(task_svc=task_svc)
    return ctx, task_svc.get(task.id), task_svc, calls


# ----------------------------------------------------------------------
# AC-6 / AC-7 / AC-8: the things it must REFUSE to do
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


# ----------------------------------------------------------------------
# AC-9 / AC-10 / AC-11: the point of the slice, and the wiring
# ----------------------------------------------------------------------

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


@pytest.mark.parametrize("decided", ["passed", "failed"])
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
