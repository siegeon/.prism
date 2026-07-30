"""Conductor v2 — verifier-driven gate decisions (issue #79 [3/4]).

Backend-only tests with a mocked VerifierService. The real verifier
shells out to pytest/ruff/etc; here we only care that ConductorService
consults it correctly and translates the verifier verdict into a gate
decision per the documented mapping (see conductor_service.py's
_VERIFIER_RULES dict).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


class FakeVerifier:
    """Records every run() call and returns a scripted result.

    Tests poke ``.next_result`` (or a list via ``.script``) before
    invoking gate_decide so we can simulate pass / fail / partial /
    error without ever shelling out to pytest.
    """

    def __init__(self, result: Optional[dict] = None) -> None:
        self.calls: list[dict] = []
        self.next_result: Optional[dict] = result
        self.script: list[dict] = []
        self.raises: Optional[BaseException] = None

    def run(self, **kwargs: object) -> dict:
        self.calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises
        if self.script:
            return self.script.pop(0)
        assert self.next_result is not None, "FakeVerifier has no result queued"
        return self.next_result


def _services(tmp_path, verifier: Optional[FakeVerifier] = None):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"),
        enable_engine=False,
        task_svc=task_svc,
        verifier_svc=verifier,
    )
    return task_svc, cond


def _workflow():
    from prism_service.models.workflow import WORKFLOW_STEPS

    return WORKFLOW_STEPS


def _walk_to_gate(cond, task_id: str, gate_id: str) -> None:
    """Advance a task until its workflow_step equals gate_id. Earlier
    pending gates are cleared with an override approve so a test that
    targets green_gate can step over red_gate without depending on the
    verifier's verdict for the earlier gate."""
    steps = _workflow()
    target_idx = next(i for i, s in enumerate(steps) if s["id"] == gate_id)
    # Walk forward up to (and including) target_idx, clearing any earlier
    # pending gate along the way with a manual override.
    from prism_service.services.task_service import TaskService  # noqa: F401
    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so this unrelated verifier-gate walk can leave review_previous_notes.
    cond._task_svc.update(task_id, premise_notes=(
        "## Premises\n- fixture walk exercising the verifier gate, not a "
        "real premise claim - UNVERIFIED\n"))
    guard = (target_idx + 1) * 3
    cleared = 0
    while guard > 0:
        guard -= 1
        snap = cond._task_svc.get(task_id)
        if snap.workflow_step == gate_id and snap.gate_state == "pending":
            return
        if snap.gate_state == "pending":
            # Proof-carrying contract (task 3826dac3): an override clearing
            # the red_gate intermediate must carry a real failing-test trace
            # + a distinct actor, else the artifact tooth rejects it.
            # DISTINCT actor per clear (task 8579d49e): with story_gate +
            # plan_gate ahead of red_gate, a reused walk actor would be a
            # same-actor self-override on the second gate and be rejected.
            cleared += 1
            cond.gate_decide(
                task_id, action="approve",
                reason=("walk_to_gate intermediate; independent re-run: "
                        "pytest -q -> 1 failed"),
                override=True, actor=f"walk-bot-{cleared}",
                session_id=f"walk-bot-{cleared}")
            continue
        cond.advance_task(task_id)


def _red_gate_id() -> str:
    return next(s["id"] for s in _workflow()
                if s["type"] == "gate" and s["id"] == "red_gate")


def _green_gate_id() -> str:
    return next(s["id"] for s in _workflow()
                if s["type"] == "gate" and s["id"] == "green_gate")


# ----------------------------------------------------------------------
# Validation-kind lookup (white-box test of the helper used by gate_decide)
# ----------------------------------------------------------------------


def test_validation_for_red_gate_is_red_with_trace():
    from prism_service.services.conductor_service import ConductorService

    assert ConductorService._validation_for_gate("red_gate") == "red_with_trace"


def test_validation_for_green_gate_is_green_full():
    from prism_service.services.conductor_service import ConductorService

    assert ConductorService._validation_for_gate("green_gate") == "green_full"


# ----------------------------------------------------------------------
# Verifier-driven approve — happy path
# ----------------------------------------------------------------------


def test_red_gate_passes_when_verifier_reports_fail_with_tier0_fail(tmp_path):
    verifier = FakeVerifier({
        "status": "fail", "tier0": "fail", "tier1": "not-run",
        "tier2": "skipped", "summary": "1/2 pass, 1 fail",
    })
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="red gate happy path")
    _walk_to_gate(cond, t.id, _red_gate_id())

    result = cond.gate_decide(
        t.id, action="approve",
        reason="test: red gate happy path; pytest -q -> 1 failed (trace)"
    )

    assert result["ok"] is True
    assert result["gate_state"] == "passed"
    assert result["validation"] == "red_with_trace"
    assert result["verifier"]["status"] == "fail"
    assert verifier.calls[0]["task_id"] == t.id


def test_red_gate_auto_advances_past_gate_after_verifier_pass(tmp_path):
    verifier = FakeVerifier({"status": "fail", "tier0": "fail",
                             "tier1": "not-run", "tier2": "skipped",
                             "summary": "red as expected"})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="auto-advance past red gate")
    _walk_to_gate(cond, t.id, _red_gate_id())

    cond.gate_decide(
        t.id, action="approve",
        reason="test: auto-advance past red gate; pytest -q -> 1 failed"
    )

    refreshed = task_svc.get(t.id)
    steps = _workflow()
    red_idx = next(i for i, s in enumerate(steps) if s["id"] == _red_gate_id())
    assert refreshed.workflow_step == steps[red_idx + 1]["id"]
    assert refreshed.gate_state == "none"


def test_green_gate_passes_when_verifier_pass_with_tier0_pass(tmp_path):
    verifier = FakeVerifier({"status": "pass", "tier0": "pass",
                             "tier1": "pass", "tier2": "skipped",
                             "summary": "all green"})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="green gate happy path")
    _walk_to_gate(cond, t.id, _green_gate_id())

    result = cond.gate_decide(
        t.id, action="approve",
        reason="test: green gate happy path; pytest -q -> 42 passed, 0 failed"
    )
    assert result["ok"] is True
    assert result["gate_state"] == "passed"
    assert result["validation"] == "green_full"


# ----------------------------------------------------------------------
# Verifier-driven approve — failure path
# ----------------------------------------------------------------------


def test_red_gate_fails_when_verifier_reports_pass(tmp_path):
    """red_with_trace expects FAILING tests. If verifier says pass,
    the failing-test scaffold did not land — gate must fail."""
    verifier = FakeVerifier({"status": "pass", "tier0": "pass",
                             "tier1": "pass", "tier2": "skipped",
                             "summary": "everything green (unexpected)"})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="red gate rejection")
    _walk_to_gate(cond, t.id, _red_gate_id())

    result = cond.gate_decide(
        t.id, action="approve", reason="test: red gate rejection"
    )

    assert result["ok"] is False
    assert result["gate_state"] == "failed"
    assert "verifier rejected" in result["reason"]
    refreshed = task_svc.get(t.id)
    assert refreshed.gate_state == "failed"
    assert refreshed.workflow_step == _red_gate_id()
    assert refreshed.gate_reason  # non-empty


def test_green_gate_fails_when_verifier_reports_fail(tmp_path):
    verifier = FakeVerifier({"status": "fail", "tier0": "fail",
                             "tier1": "not-run", "tier2": "skipped",
                             "summary": "2 tests failing"})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="green gate rejection")
    _walk_to_gate(cond, t.id, _green_gate_id())

    result = cond.gate_decide(
        t.id, action="approve", reason="test: green gate rejection"
    )

    assert result["ok"] is False
    assert result["gate_state"] == "failed"
    assert "2 tests failing" in result["reason"]


def test_green_gate_fails_when_verifier_partial(tmp_path):
    """green_full expects every check to pass; partial means at least
    one fail mixed with passes, which is still incomplete."""
    verifier = FakeVerifier({"status": "partial", "tier0": "fail",
                             "tier1": "pass", "tier2": "skipped",
                             "summary": "1 lint failure"})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="green partial")
    _walk_to_gate(cond, t.id, _green_gate_id())

    result = cond.gate_decide(
        t.id, action="approve", reason="test: green partial"
    )
    assert result["ok"] is False
    assert result["gate_state"] == "failed"


# ----------------------------------------------------------------------
# Audit trail — task_history must surface the verifier verdict
# ----------------------------------------------------------------------


def test_failed_gate_records_verifier_reason_in_task_history(tmp_path):
    verifier = FakeVerifier({"status": "pass", "tier0": "pass",
                             "tier1": "pass", "tier2": "skipped",
                             "summary": "premature green"})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="history captures verifier reason")
    _walk_to_gate(cond, t.id, _red_gate_id())

    cond.gate_decide(
        t.id, action="approve", reason="test: history captures verifier reason"
    )

    rows = task_svc.history(t.id)
    gate_rows = [r for r in rows if r.action == "gate_decide"]
    assert gate_rows, "gate_decide must produce a task_history row"
    last = gate_rows[-1]
    assert "verifier=fail" in last.details
    assert "validation=red_with_trace" in last.details
    assert last.actor == "conductor"


def test_passed_gate_records_verifier_pass_in_task_history(tmp_path):
    verifier = FakeVerifier({"status": "fail", "tier0": "fail",
                             "tier1": "not-run", "tier2": "skipped",
                             "summary": "red as expected"})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="history captures verifier pass")
    _walk_to_gate(cond, t.id, _red_gate_id())

    cond.gate_decide(
        t.id, action="approve",
        reason="test: history captures verifier pass; pytest -q -> 1 failed"
    )

    rows = task_svc.history(t.id)
    gate_rows = [r for r in rows if r.action == "gate_decide"]
    assert any("verifier=pass" in r.details for r in gate_rows)


# ----------------------------------------------------------------------
# Manual override path
# ----------------------------------------------------------------------


def test_override_bypasses_verifier_and_passes_gate(tmp_path):
    """Even if the verifier would reject, override=True forces pass."""
    verifier = FakeVerifier({"status": "pass", "tier0": "pass",
                             "tier1": "pass", "tier2": "skipped",
                             "summary": "would have failed red gate"})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="manual override")
    _walk_to_gate(cond, t.id, _red_gate_id())

    result = cond.gate_decide(
        t.id, action="approve",
        reason="qa says it's fine; independent re-run: pytest -q -> 1 failed",
        override=True,
    )
    assert result["ok"] is True
    assert result["gate_state"] == "passed"
    assert result["override"] is True
    # Verifier must not have been consulted on the override path.
    assert verifier.calls == []


def test_override_history_row_is_tagged_manual_override(tmp_path):
    verifier = FakeVerifier({"status": "pass", "tier0": "pass",
                             "tier1": "pass", "tier2": "skipped",
                             "summary": ""})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="override audit")
    _walk_to_gate(cond, t.id, _red_gate_id())

    cond.gate_decide(t.id, action="approve",
                     reason="urgent ship; independent re-run: pytest -> 1 failed",
                     override=True)

    rows = task_svc.history(t.id)
    gate_rows = [r for r in rows if r.action == "gate_decide"]
    assert gate_rows, "override must still produce an audit row"
    last = gate_rows[-1]
    assert last.actor == "manual-override"
    assert "override=True" in last.details
    assert "urgent ship" in last.details


# ----------------------------------------------------------------------
# Edge cases: no verifier, verifier raises, reject path still works
# ----------------------------------------------------------------------


def test_approve_without_verifier_attached_falls_back_to_legacy_trust(tmp_path):
    """Backward-compat: bare ConductorService (no VerifierService) keeps
    the [1/4] behavior — approve passes the gate on the caller's word.
    ProjectContext always wires a verifier in production; this path
    exists for the meta-only unit tests."""
    task_svc, cond = _services(tmp_path, verifier=None)
    t = task_svc.create(title="legacy trust-caller mode")
    _walk_to_gate(cond, t.id, _red_gate_id())

    result = cond.gate_decide(
        t.id, action="approve",
        reason="test: legacy trust-caller; pytest -q -> 1 failed"
    )
    assert result["ok"] is True
    assert result["gate_state"] == "passed"
    assert "verifier" not in result


def test_verifier_exception_fails_gate_gracefully(tmp_path):
    verifier = FakeVerifier()
    verifier.raises = RuntimeError("subprocess exploded")
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="verifier blew up")
    _walk_to_gate(cond, t.id, _red_gate_id())

    result = cond.gate_decide(
        t.id, action="approve", reason="test: verifier blew up"
    )
    assert result["ok"] is False
    assert result["gate_state"] == "failed"
    assert "subprocess exploded" in result["reason"]


def test_reject_path_unchanged_by_verifier_hookup(tmp_path):
    """reject must not consult the verifier — it's an explicit human
    'no'. Backward-compat with [1/4] behavior."""
    verifier = FakeVerifier({"status": "pass", "tier0": "pass",
                             "tier1": "pass", "tier2": "skipped",
                             "summary": ""})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="reject still works")
    _walk_to_gate(cond, t.id, _red_gate_id())

    result = cond.gate_decide(t.id, action="reject", reason="design flaw")
    assert result["ok"] is True
    assert result["gate_state"] == "failed"
    assert verifier.calls == []
    refreshed = task_svc.get(t.id)
    assert refreshed.gate_reason == "design flaw"


def test_late_attach_verifier_service(tmp_path):
    """attach_verifier_service swaps the verifier in after construction."""
    task_svc, cond = _services(tmp_path, verifier=None)
    t = task_svc.create(title="late-bind verifier")
    _walk_to_gate(cond, t.id, _red_gate_id())

    late = FakeVerifier({"status": "fail", "tier0": "fail",
                         "tier1": "not-run", "tier2": "skipped",
                         "summary": "red"})
    cond.attach_verifier_service(late)
    result = cond.gate_decide(
        t.id, action="approve",
        reason="test: late-bind verifier; pytest -q -> 1 failed"
    )
    assert result["ok"] is True
    assert late.calls and late.calls[0]["task_id"] == t.id
