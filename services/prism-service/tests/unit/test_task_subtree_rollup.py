"""RED scaffold — tasks render and roll up to ANY depth (task e72aba6d).

PRISM stores a task tree via parent_id, but every consumer today looks ONE
level deep: the detail API/page list only DIRECT children, and the epic
green_gate roll-up considers only DIRECT children — so deep fanout is invisible
past the first hop and a grandchild's incompleteness cannot block a
grandparent.

Pins, RED-first (these FAIL before descendants()/descendant_counts(), the
subtree-annotated detail API, and the subtree-aware epic_rollup_verdict land):

  * AC-1 — TaskService.descendants(id) walks the WHOLE subtree (transitive
    children, task excluded) breadth-first and is cycle-safe; descendant_counts
    returns (total, done) over that subtree.
  * AC-2 — GET /api/tasks/{id} carries descendant_count + descendant_done for
    the whole subtree AND annotates each DIRECT child with its own child_count.
  * AC-3 — a 3-level tree (grandparent > parent > child) gates green ONLY when
    every descendant is done: an incomplete grandchild blocks the grandparent's
    green_gate; an intermediate node with weak proof does NOT block once its
    leaf descendants are done+strong (leaf-scoped proof).
"""

from __future__ import annotations

import asyncio  # noqa: F401  (imported for parity with sibling gate tests)
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ── AC-1: subtree walk + counts on TaskService ─────────────────────────

def _tree(tmp_path):
    """g > p > c1, c2 ; and p2 (a second branch under g). Returns the svc
    plus the created tasks so callers can mutate status."""
    from prism_service.services.task_service import TaskService
    svc = TaskService(str(tmp_path / "tasks.db"))
    g = svc.create(title="grandparent")
    p = svc.create(title="parent", parent_id=g.id)
    c1 = svc.create(title="child-1", parent_id=p.id)
    c2 = svc.create(title="child-2", parent_id=p.id)
    p2 = svc.create(title="parent-2", parent_id=g.id)
    return svc, g, p, c1, c2, p2


def test_descendants_walks_whole_subtree(tmp_path):
    svc, g, p, c1, c2, p2 = _tree(tmp_path)
    ids = {t.id for t in svc.descendants(g.id)}
    # The WHOLE subtree (not just the two direct children p, p2).
    assert ids == {p.id, c1.id, c2.id, p2.id}, ids
    # The task itself is never in its own descendant set.
    assert g.id not in ids
    # A mid-tree node returns only ITS subtree.
    assert {t.id for t in svc.descendants(p.id)} == {c1.id, c2.id}
    # A leaf has no descendants.
    assert svc.descendants(c1.id) == []


def test_descendants_is_cycle_safe(tmp_path):
    svc, g, p, c1, c2, p2 = _tree(tmp_path)
    # Corrupt the store directly (bypassing _validate_parent) to close a
    # cycle g -> ... -> c1 -> g, then prove the walk TERMINATES via the
    # visited-guard instead of hanging.
    svc._db.execute("UPDATE tasks SET parent_id=? WHERE id=?", (c1.id, g.id))
    svc._db.commit()
    ids = {t.id for t in svc.descendants(g.id)}
    assert g.id not in ids  # the walk must not re-add the root through the cycle
    assert c1.id in ids


def test_descendant_counts_totals_and_done(tmp_path):
    svc, g, p, c1, c2, p2 = _tree(tmp_path)
    svc.update(c1.id, status="done")
    total, done = svc.descendant_counts(g.id)
    assert total == 4 and done == 1, (total, done)
    # Cancelled descendants drop out of BOTH totals (never block a roll-up).
    svc.update(c2.id, status="cancelled")
    total, done = svc.descendant_counts(g.id)
    assert total == 3 and done == 1, (total, done)


# ── AC-2: subtree annotations on the detail API ────────────────────────

@pytest.fixture
def project(tmp_path, monkeypatch):
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield "test-e72aba6d"
    pc._contexts.clear()


def test_detail_api_exposes_subtree_counts_and_child_count(project):
    from prism_service.api.tasks import get_task
    from prism_service.project_context import get_project
    svc = get_project(project).task_svc
    g = svc.create(title="grandparent")
    p = svc.create(title="parent", parent_id=g.id)
    c1 = svc.create(title="child-1", parent_id=p.id)
    svc.create(title="child-2", parent_id=p.id)
    svc.update(c1.id, status="done")

    out = get_task(g.id, project=project)
    # Whole-subtree roll-up (grandparent has 3 descendants, 1 done).
    assert out["descendant_count"] == 3, out.get("descendant_count")
    assert out["descendant_done"] == 1, out.get("descendant_done")
    # Direct children are annotated with their OWN subtree size so the UI
    # can lazy-render a tree node's chevron without re-walking the list.
    children = {c["id"]: c for c in out["children"]}
    assert set(children) == {p.id}
    assert children[p.id]["child_count"] == 2, children[p.id]


# ── AC-3: subtree-aware epic roll-up (pure verdict) ────────────────────

class _Node:
    def __init__(self, id, status, proof, parent_id):
        self.id = id
        self.status = status
        self.completion_proof = proof
        self.parent_id = parent_id


_STRONG = "pytest -q: 9 passed; screenshot :8888/x.png"


def test_rollup_blocks_while_a_grandchild_is_incomplete():
    from prism_service.services.conductor_service import epic_rollup_verdict
    # Grandparent's whole subtree: parent (done, strong) + child (NOT done).
    subtree = [
        _Node("p", "done", _STRONG, "g"),
        _Node("c", "in_progress", _STRONG, "p"),
    ]
    ok, reason = epic_rollup_verdict(subtree)
    assert ok is False, reason
    assert "not done" in reason.lower() or "child" in reason.lower()


def test_rollup_passes_when_whole_subtree_done_even_if_intermediate_proof_weak():
    from prism_service.services.conductor_service import epic_rollup_verdict
    # Every descendant done; the INTERMEDIATE node carries NO proof of its own
    # (its proof is its children's roll-up) — only the LEAF needs strong proof.
    subtree = [
        _Node("p", "done", "", "g"),           # intermediate, weak/absent proof
        _Node("c", "done", _STRONG, "p"),      # leaf, strong proof
    ]
    ok, reason = epic_rollup_verdict(subtree)
    assert ok is True, reason


def test_rollup_still_fails_on_weak_leaf_proof():
    from prism_service.services.conductor_service import epic_rollup_verdict
    subtree = [
        _Node("p", "done", _STRONG, "g"),
        _Node("cccc1234", "done", "", "p"),    # leaf with weak/absent proof
    ]
    ok, reason = epic_rollup_verdict(subtree)
    assert ok is False
    assert "proof" in reason.lower()


# ── AC-3 integration: subtree roll-up rides the REAL green_gate ────────

def _services(tmp_path):
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False, task_svc=task_svc)
    return task_svc, cond


def _drive_to_green_gate(task_svc, cond, t):
    guard = 0
    cleared = 0
    while task_svc.get(t.id).workflow_step != "green_gate" and guard < 30:
        if task_svc.get(t.id).gate_state == "pending":
            cleared += 1
            cond.gate_decide(
                t.id, "approve",
                reason="walk; independent re-run: pytest -> 1 failed",
                override=True, actor=f"walk-bot-{cleared}",
                session_id=f"walk-bot-{cleared}")
        else:
            cond.advance_task(t.id, validation="step")
        guard += 1
    assert task_svc.get(t.id).workflow_step == "green_gate"


def test_grandparent_green_gate_blocks_on_incomplete_grandchild(tmp_path):
    task_svc, cond = _services(tmp_path)
    g = task_svc.create(title="grandparent epic")
    p = task_svc.create(title="parent epic", parent_id=g.id)
    task_svc.update(p.id, status="done",
                    completion_proof="pytest -q: 9 passed; :8888/p.png")
    # A grandchild that is NOT done — invisible to a one-level roll-up.
    gc = task_svc.create(title="grandchild", parent_id=p.id)

    _drive_to_green_gate(task_svc, cond, g)
    res = cond.gate_decide(g.id, "approve", reason="try to close the epic")
    assert res["ok"] is False, res
    assert task_svc.get(g.id).gate_state != "passed"

    # Complete the grandchild with strong proof; now the whole subtree is done.
    task_svc.update(gc.id, status="done",
                    completion_proof="pytest -q: 9 passed; :8888/gc.png")
    res2 = cond.gate_decide(g.id, "approve", reason="whole subtree complete")
    assert res2["ok"] is True, res2
