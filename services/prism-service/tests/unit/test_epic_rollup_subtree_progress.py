"""An epic-rollup gate must answer "how much work is actually done" at a
glance, across the WHOLE descendant subtree -- not just direct children.

Owner, live, watching task 95474ec7 (a 4-level epic-of-epics: 95474ec7 ->
3a3f90da -> 0e2c82f3 -> 9b0f7c4b, plus siblings): "look at it as a user its
impossobvle to see at this top level how much work is actualy done we have
been waitinv fo almsot 2 hours acvcoriting to what i see". The live answer
was NOT stalled -- 3 of 7 descendants were fully done and 2 more had just
reached their own green_gate pending only a merge -- but nothing on the
epic page said so; `blocking_children` (existing) only names the ONE unfinished
DIRECT child, so a reader has to click through several levels by hand to see
real progress.

subtree_progress_counts() recursively counts every live (non-cancelled,
non-deleted) descendant's status, bounded depth 6 (matches
ConductorService._subtree_active's own bound), and gate_readiness's
epic-rollup branch attaches it to the response as `subtree_progress` so the
UI can render it without an extra round trip.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _task_svc(tmp_path):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / "tasks.db"))


def _mk(task_svc, title, parent_id="", status="pending"):
    t = task_svc.create(title=title, parent_id=parent_id)
    task_svc.update(t.id, status=status)
    return t


def test_counts_the_whole_subtree_not_just_direct_children(tmp_path):
    from prism_service.services.conductor_service import subtree_progress_counts

    task_svc = _task_svc(tmp_path)
    root = _mk(task_svc, "epic", status="in_progress")
    done1 = _mk(task_svc, "done sibling 1", root.id, status="done")
    done2 = _mk(task_svc, "done sibling 2", root.id, status="done")
    mid = _mk(task_svc, "mid epic", root.id, status="in_progress")
    grand = _mk(task_svc, "grandchild epic", mid.id, status="in_progress")
    leaf1 = _mk(task_svc, "leaf blocked on ship", grand.id, status="blocked")
    leaf2 = _mk(task_svc, "leaf sibling", mid.id, status="blocked")

    counts = subtree_progress_counts(task_svc, root.id)
    assert counts["total"] == 6, counts  # everyone under root, NOT root itself
    assert counts["done"] == 2, counts
    assert counts["in_progress"] == 2, counts  # mid, grand
    assert counts["blocked"] == 2, counts  # leaf1, leaf2


def test_cancelled_and_deleted_children_are_excluded(tmp_path):
    from prism_service.services.conductor_service import subtree_progress_counts

    task_svc = _task_svc(tmp_path)
    root = _mk(task_svc, "epic", status="in_progress")
    _mk(task_svc, "real child", root.id, status="done")
    dead = _mk(task_svc, "cancelled child", root.id, status="pending")
    task_svc.update(dead.id, status="cancelled")

    counts = subtree_progress_counts(task_svc, root.id)
    assert counts["total"] == 1, counts
    assert counts["done"] == 1, counts


def test_a_leaf_task_with_no_children_counts_as_empty_subtree(tmp_path):
    from prism_service.services.conductor_service import subtree_progress_counts

    task_svc = _task_svc(tmp_path)
    leaf = _mk(task_svc, "childless task", status="in_progress")
    counts = subtree_progress_counts(task_svc, leaf.id)
    assert counts["total"] == 0, counts


def test_gate_readiness_epic_rollup_attaches_subtree_progress(tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    root = task_svc.create(title="epic under review")
    task_svc.update(root.id, workflow_step="green_gate", gate_state="pending")
    done_child = task_svc.create(title="finished", parent_id=root.id,
                                 oracle="x", proof_type="test", likely_misfire="y")
    task_svc.update(done_child.id, status="done",
                    completion_proof="pytest tests/unit/test_a.py -q -> 3 passed")
    blocking = task_svc.create(title="still working", parent_id=root.id,
                               oracle="x", proof_type="test", likely_misfire="y")
    task_svc.update(blocking.id, status="in_progress")
    grandchild = task_svc.create(title="grandchild", parent_id=blocking.id,
                                 oracle="x", proof_type="test", likely_misfire="y")
    task_svc.update(grandchild.id, status="blocked")

    out = capi.gate_readiness(task_id=root.id, project="default")
    assert out["receipt"]["adapter"] == "epic-rollup", out
    assert "subtree_progress" in out, out
    sp = out["subtree_progress"]
    assert sp["total"] == 3, sp  # done_child, blocking, grandchild
    assert sp["done"] == 1, sp
    assert sp["in_progress"] == 1, sp
    assert sp["blocked"] == 1, sp


def test_task_detail_renders_subtree_progress_summary():
    """Source-level pin (the PRISM SPA has no JS test runner, same
    convention as test_conductor_page_animated_cleanup_ui.py): the epic
    banner must render subtree_progress when present."""
    from pathlib import Path
    here = Path(__file__).resolve()
    src = (here.parent.parent.parent / "prism_service" / "web" / "src"
          / "pages" / "TaskDetailPage.tsx").read_text(encoding="utf-8")
    assert "gateReadiness?.subtree_progress" in src or "gateReadiness.subtree_progress" in src, (
        "TaskDetailPage.tsx must render gateReadiness.subtree_progress "
        "somewhere so the epic banner shows aggregate descendant progress")
    assert "subtree progress" in src.lower()
