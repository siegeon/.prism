"""An epic-rollup receipt must never close a human-only demo/review gate
(child 457b38db; epic 37c9207b oracle clause; mx-7e03ff/mx-e2868f).

THE BUG: ConductorService.gate_decide's epic roll-up branch
(services/prism-service/prism_service/services/conductor_service.py, the
`elif rollup_ok:` arm around line 3535) approves a parent's green_gate
purely on `epic_rollup_verdict(children)` -- every non-cancelled child done
with a strong completion_proof -- with NO check of the PARENT's own
proof_type. Owner rule eaafdf75 requires proof_type in ("demo", "review") to
be human-only: adjudicate_green_gate enforces this by never calling
gate_decide for an epic in the first place (`if
self._task_svc.list(parent_id=task_id): return None`), but that guard lives
at the MACHINE-ADJUDICATOR CALL SITE, not inside gate_decide itself. Any
OTHER caller of gate_decide (a driving agent session reporting through
conductor_work, a scripted approve) reaches the rollup arm directly and
closes a demo/review epic on child status alone -- reproduced live
2026-08-10 on task 64ba4755 (mx-7e03ff): gate_decide history row verbatim
"gate=green_gate; action=approve; epic-rollup=pass; reason=Approving on
live evidence: fresh passing oracle receipt (epic-rollup) + drive record."
with no human ever reviewing the demo.

Pins, RED-first (current source lets AC-1/AC-2 pass with ok=True -- the bug):
  AC-1  a proof_type=demo epic whose children are ALL done with strong
        completion_proof is REFUSED (ok is False, gate_state stays
        "pending", never "passed") when approved via a plain distinct-actor
        call carrying only the rollup rationale -- the same call shape that
        closed 64ba4755 live.
  AC-2  the SAME call on a proof_type=review epic is refused identically.
  AC-3  ANTI-REGRESSION: the identical call on a proof_type=test (non
        human-judgment) epic still passes via rollup -- the fix must not
        break the genuine, already-correct roll-up path for
        machine-graded epics.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


_STRONG_CHILD_PROOF = ("pytest tests/unit/test_x.py -q -> 5 passed, 0 "
                       "failed; all green")

_ROLLUP_REASON = ("Approving on live evidence: fresh passing oracle "
                  "receipt (epic-rollup) + drive record.")


def _parent(task_svc, title: str, proof_type: str):
    t = task_svc.create(title=title, tags=["conductor"],
                        oracle="a person reviews the demo end to end",
                        proof_type=proof_type,
                        likely_misfire="child status stands in for the "
                                      "owner's own review")
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending")
    return t


def _done_child(task_svc, parent_id: str, title: str):
    c = task_svc.create(title=title, parent_id=parent_id, oracle="x",
                        proof_type="test", likely_misfire="y",
                        completion_proof=_STRONG_CHILD_PROOF)
    task_svc.update(c.id, status="done")
    return c


# ---------------------------------------------------------------------------
# AC-1 -- a demo epic never closes on rollup alone
# ---------------------------------------------------------------------------


def test_demo_epic_rollup_alone_never_closes_the_gate(tmp_path):
    task_svc, cond = _services(tmp_path)
    parent = _parent(task_svc, "epic with all children done", "demo")
    _done_child(task_svc, parent.id, "slice one")
    _done_child(task_svc, parent.id, "slice two")

    out = cond.gate_decide(parent.id, "approve", reason=_ROLLUP_REASON,
                           session_id="distinct-drive-session",
                           actor="distinct-drive-session")

    assert out["ok"] is False, out
    live = task_svc.get(parent.id)
    assert live.gate_state != "passed", (out, live.gate_state)


# ---------------------------------------------------------------------------
# AC-2 -- same for proof_type=review
# ---------------------------------------------------------------------------


def test_review_epic_rollup_alone_never_closes_the_gate(tmp_path):
    task_svc, cond = _services(tmp_path)
    parent = _parent(task_svc, "review epic with all children done", "review")
    _done_child(task_svc, parent.id, "slice one")

    out = cond.gate_decide(parent.id, "approve", reason=_ROLLUP_REASON,
                           session_id="distinct-drive-session",
                           actor="distinct-drive-session")

    assert out["ok"] is False, out
    live = task_svc.get(parent.id)
    assert live.gate_state != "passed", (out, live.gate_state)


# ---------------------------------------------------------------------------
# AC-3 -- anti-regression: a test-type epic still rolls up cleanly
# ---------------------------------------------------------------------------


def test_test_type_epic_rollup_still_closes_the_gate(tmp_path):
    task_svc, cond = _services(tmp_path)
    parent = _parent(task_svc, "machine-graded epic with all children done",
                     "test")
    _done_child(task_svc, parent.id, "slice one")

    out = cond.gate_decide(parent.id, "approve", reason=_ROLLUP_REASON,
                           session_id="distinct-drive-session",
                           actor="distinct-drive-session")

    assert out["ok"] is True, out
    live = task_svc.get(parent.id)
    assert live.gate_state == "passed", (out, live.gate_state)
