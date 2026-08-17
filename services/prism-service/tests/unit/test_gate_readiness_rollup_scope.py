"""A cancelled child cannot fake an epic at the gate (task 16388421).

THE BUG: GET /api/conductor/gate/readiness (api/conductor.py:gate_readiness)
reads a task's children raw via `kids = list(s._task_svc.list(parent_id=
task_id))` then branches into the epic-rollup adapter on the bare truthiness
`if kids:` — a task whose ONLY child is cancelled (or soft-deleted) still has
a non-empty `kids` list, so it enters the epic-rollup branch even though
epic_rollup_verdict() itself correctly excludes cancelled children and would
say "not an epic" if given the chance to decide the WHOLE branch. Because the
branch decision (`if kids:`) and the verdict decision (epic_rollup_verdict)
disagree about what counts as a live child, a proof_type=demo task with a
single dead child renders a false "BLOCKED · rollup_blocked" banner instead
of falling through to the clean human-judgment Approve it should reach.
blocking_children has the identical raw-read bug: it is built from the same
unfiltered `kids`, and only excludes literal status=="cancelled" — a
soft-deleted child (status=="deleted") still gets named as a blocker.

Repro subject: task 4e6c4bf3-b38f-43a8-a34f-0191f3cac6e1 (one cancelled
child aa8e82be) — reproduced here with disposable fixtures, never the live
daemon's tasks.db.

Pins, RED-first (ALL FAIL against current source):
  AC-1  a demo task whose ONLY child is cancelled gets adapter=="human",
        status=="your_review", receipt_ok True, manual_review True, and no
        "rollup" string anywhere in the JSON response.
  AC-2  a demo task whose ONLY child is soft-deleted (status=="deleted") is
        treated exactly like AC-1 — adapter != "epic-rollup".
  AC-3  a task with one LIVE child (pending) is UNCHANGED: adapter==
        "epic-rollup", status=="rollup_blocked", reason names the
        unfinished-child count — no regression to the genuine epic path.
  AC-6  blocking_children is derived from the SAME live-child-filtered list:
        a cancelled/deleted child's id never appears in blocking_children
        while a live sibling's id does.
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


# A demo/visual green_gate task with a real test-runner + pass signal in its
# completion_proof, so the human-judgment branch's artifact tooth is
# satisfied (art=="") and the READY case renders, not "needs_visual_evidence".
_DEMO_PROOF = ("pytest tests/unit/test_x.py -q -> 3 passed, 0 failed; "
              "all green")


def _demo_parent(task_svc, title: str):
    t = task_svc.create(title=title, tags=["conductor"],
                        oracle="the human reviews the demo",
                        proof_type="demo",
                        likely_misfire="a dead child fakes an epic",
                        completion_proof=_DEMO_PROOF)
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending")
    return t


def _epic_parent(task_svc, title: str):
    t = task_svc.create(title=title, tags=["conductor"],
                        oracle="children finish", proof_type="test",
                        likely_misfire="rolls up without real evidence")
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending")
    return t


# ---------------------------------------------------------------------------
# AC-1 — cancelled-only child falls through to human-judgment, not rollup
# ---------------------------------------------------------------------------


def test_cancelled_only_child_falls_through_to_human_judgment(
        tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    task_svc, cond = _services(tmp_path)
    parent = _demo_parent(task_svc, "demo task with one dead child")
    dead = task_svc.create(title="abandoned slice", parent_id=parent.id,
                           oracle="x", proof_type="test", likely_misfire="y")
    task_svc.update(dead.id, status="cancelled")
    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    out = capi.gate_readiness(task_id=parent.id, project="default")

    assert out["receipt"]["adapter"] == "human", out
    assert out["receipt"]["status"] == "your_review", out
    assert out["receipt_ok"] is True, out
    assert out.get("manual_review") is True, out
    import json
    assert "rollup" not in json.dumps(out).lower(), out


# ---------------------------------------------------------------------------
# AC-2 — soft-deleted-only child is treated exactly like cancelled
# ---------------------------------------------------------------------------


def test_deleted_only_child_falls_through_to_human_judgment(
        tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    task_svc, cond = _services(tmp_path)
    parent = _demo_parent(task_svc, "demo task with one soft-deleted child")
    dead = task_svc.create(title="removed slice", parent_id=parent.id,
                           oracle="x", proof_type="test", likely_misfire="y")
    task_svc.update(dead.id, status="deleted")
    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    out = capi.gate_readiness(task_id=parent.id, project="default")

    assert out["receipt"]["adapter"] != "epic-rollup", out
    assert out["receipt"]["adapter"] == "human", out
    assert out["receipt_ok"] is True, out


# ---------------------------------------------------------------------------
# AC-3 — one LIVE child still gets the genuine epic-rollup path (no regression)
# ---------------------------------------------------------------------------


def test_one_live_child_still_gets_epic_rollup_blocked(tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    task_svc, cond = _services(tmp_path)
    parent = _epic_parent(task_svc, "epic with one unfinished live child")
    live = task_svc.create(title="in-flight slice", parent_id=parent.id,
                           oracle="x", proof_type="test", likely_misfire="y")
    task_svc.update(live.id, status="pending")
    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    out = capi.gate_readiness(task_id=parent.id, project="default")

    assert out["receipt"]["adapter"] == "epic-rollup", out
    assert out["receipt"]["status"] == "rollup_blocked", out
    assert out["receipt_ok"] is False, out
    assert "1 child task" in out["receipt"]["reason"], out
    assert "not done" in out["receipt"]["reason"], out


# ---------------------------------------------------------------------------
# AC-6 — blocking_children excludes dead siblings, names the live one
# ---------------------------------------------------------------------------


def test_blocking_children_excludes_dead_and_names_live_sibling(
        tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    task_svc, cond = _services(tmp_path)
    parent = _epic_parent(task_svc, "epic with a dead sibling and a live one")
    dead = task_svc.create(title="soft-deleted sibling", parent_id=parent.id,
                           oracle="x", proof_type="test", likely_misfire="y")
    task_svc.update(dead.id, status="deleted")
    live = task_svc.create(title="still-in-flight sibling",
                           parent_id=parent.id, oracle="x",
                           proof_type="test", likely_misfire="y")
    task_svc.update(live.id, status="in_progress")
    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    out = capi.gate_readiness(task_id=parent.id, project="default")

    blockers = out.get("blocking_children") or []
    ids = {b["id"] for b in blockers}
    assert live.id in ids, out
    assert dead.id not in ids, out
