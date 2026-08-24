"""verify_green_state must not advance a demo/review-proof task to
green_gate with zero captured evidence (task 3baadd19 qa discovery,
2026-08-24).

LIVE REGRESSION this pins: task 3baadd19 ("PRISM drives its own tasks end
to end", proof_type=demo) reported SUCCESS at verify_green_state with a
completion_proof that explicitly admitted the epic's own oracle was NOT
met ("the epic's actual oracle... is NOT yet true in production because
task_runner.py doesn't pass timeout_s") and with ZERO captured evidence in
the task's evidence store (the Decision Packet's "Visual evidence" row
read "none") -- and it advanced to green_gate anyway, landing on
"AWAITING YOUR REVIEW... Approve to release" as if it were ready.

Owner, live on this exact task: "well that seems like a fundamental flaw
doesn't it" -- "the conductor workflow session that ran this task should
not have allowed that... find that break."

Root cause: ui_artifact_gate_reason (conductor_service.py, STRAND C) is
the only existing tooth requiring a real captured artifact before a demo
claim can close, but it is scoped to `"ui" in tags` -- a proof_type=demo
task with NO "ui" tag (this epic: tags=conductor/architecture/
owner-directive/drive-worker/github/jira) never trips it. And that tooth
only ever runs at green_gate's OWN decision (gate_decide/
adjudicate_green_gate) -- nothing checks it at the EARLIER point where the
workflow session's own verify_green_state SUCCESS report advances the
task INTO green_gate in the first place (conductor_flow.flow_report ->
ConductorService.advance_task). A demo task with no "ui" tag could sail
all the way to "ready for your review" carrying nothing to review.

Fix: advance_task now refuses a verify_green_state advance when the task's
oracle is human-judgment (proof_type in ("demo","review"), or any
browser/manual oracle per oracle_spec.is_human_judgment) AND
has_captured_evidence() is False -- checked at the ONE choke point every
caller (flow_report's server-driven loop, the legacy conductor_advance MCP
tool) passes through, not patched per-caller.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _services(tmp_path):
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    cond._project_name = "testproj"
    return task_svc, cond


_ADMITS_ORACLE_NOT_MET = (
    "13/13 passed genuinely. Verdict: the epic's actual oracle... is NOT "
    "yet true in production because the wiring gap means nothing calls "
    "this with a bound yet."
)


def _demo_task_at_verify_green_state(task_svc, *, tags=None):
    """Replays task 3baadd19's own shape: a NON-"ui"-tagged proof_type=demo
    task sitting at verify_green_state, ready to report SUCCESS -- exactly
    where the live bug was reproduced."""
    t = task_svc.create(
        title="PRISM drives its own tasks end to end",
        tags=tags if tags is not None else
            ["conductor", "architecture", "owner-directive", "drive-worker"],
        oracle=("PRISM claims a task by itself; film/screenshots of the "
               "unattended drive in the PRISM evidence store, cited in "
               "completion_proof"),
        proof_type="demo",
        completion_proof=_ADMITS_ORACLE_NOT_MET,
    )
    task_svc.update(t.id, workflow_step="verify_green_state")
    return task_svc.get(t.id)


def test_A1_demo_task_with_no_ui_tag_and_no_evidence_cannot_advance(
    tmp_path,
):
    """AC-1, the exact live shape: proof_type=demo, no "ui" tag, zero
    captured evidence -- the advance must be REFUSED."""
    task_svc, cond = _services(tmp_path)
    task = _demo_task_at_verify_green_state(task_svc)

    result = cond.advance_task(task.id, session_id="drive-session")

    assert result["ok"] is False, (
        f"a proof_type=demo task with NO captured evidence advanced past "
        f"verify_green_state -- this is the exact live 3baadd19 bug: "
        f"{result}")
    after = task_svc.get(task.id)
    assert after.workflow_step == "verify_green_state", (
        "must stay parked at verify_green_state, never silently reach "
        f"green_gate — got {after.workflow_step!r}")
    reason = (result.get("reason") or "").lower()
    assert "evidence" in reason or "demonstrat" in reason


def test_A2_reason_is_recorded_so_a_driver_can_self_diagnose(tmp_path):
    """A refused advance must record an actionable gate_reason (task
    8f48f9bb precedent: never a silent/empty refusal a driver can't act
    on)."""
    task_svc, cond = _services(tmp_path)
    task = _demo_task_at_verify_green_state(task_svc)

    cond.advance_task(task.id, session_id="drive-session")

    after = task_svc.get(task.id)
    assert (after.gate_reason or "").strip(), (
        "a refused verify_green_state advance left gate_reason empty — a "
        "driving session cannot self-diagnose why it is stuck")


def test_B1_captured_evidence_lets_the_same_task_advance(tmp_path):
    """Anti-over-strictness: once real evidence IS captured, the identical
    task must advance cleanly — this tooth narrows, it never becomes a
    blanket block on every demo task."""
    from prism_service.data_dir import evidence_dir

    task_svc, cond = _services(tmp_path)
    task = _demo_task_at_verify_green_state(task_svc)
    d = evidence_dir(task.id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "drive.png").write_bytes(b"\x89PNG\r\n\x1a\n0000")

    result = cond.advance_task(task.id, session_id="drive-session")

    assert result["ok"] is True, (
        f"captured evidence exists but the advance was still refused: "
        f"{result}")
    after = task_svc.get(task.id)
    assert after.workflow_step == "green_gate", (
        f"expected to reach green_gate, got {after.workflow_step!r}")


def test_B2_a_test_type_task_is_unaffected(tmp_path):
    """Anti-regression: a machine-graded (proof_type=test) task at
    verify_green_state must be completely untouched by this tooth — it is
    not a human-judgment claim, so evidence is not its business."""
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(
        title="backend fix", tags=["backend"], oracle="",
        proof_type="test",
        completion_proof="tests/unit/test_x.py::test_y PASSED",
    )
    task_svc.update(t.id, workflow_step="verify_green_state")

    result = cond.advance_task(t.id, session_id="drive-session")

    assert result["ok"] is True, (
        f"a proof_type=test task was wrongly blocked by the demo-evidence "
        f"tooth: {result}")


def test_B3_a_ui_tagged_demo_task_is_still_governed_the_same_way(tmp_path):
    """Anti-regression: this new check and the existing ui_artifact_gate_
    reason tooth must not conflict — a ui-tagged demo task with no
    evidence is refused by (at minimum) this new check just as reliably as
    a non-ui-tagged one."""
    task_svc, cond = _services(tmp_path)
    task = _demo_task_at_verify_green_state(task_svc, tags=["ui"])

    result = cond.advance_task(task.id, session_id="drive-session")

    assert result["ok"] is False, result


def test_C1_source_pins_the_check_lives_in_advance_task_itself(tmp_path):
    """Pin WHERE the fix lives: advance_task, not a per-caller patch in
    conductor_flow.py -- so every caller (flow_report, the legacy
    conductor_advance MCP tool) inherits it for free."""
    import inspect

    from prism_service.services.conductor_service import ConductorService

    src = inspect.getsource(ConductorService.advance_task)
    assert "verify_green_state" in src
    assert "has_captured_evidence" in src
