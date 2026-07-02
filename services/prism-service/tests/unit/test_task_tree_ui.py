"""UI contract — recursive collapsible task tree + descendant board badge
(task e72aba6d, "tasks render and roll up to any depth").

The PRISM SPA has no JS test runner, so the UI-FIRST acceptance criteria are
pinned by asserting the ACTUAL web source (TSX) — the same pattern as
tests/unit/test_animate_conductor_tasks_ui.py and the sibling *_ui.py contracts.

These assert the real seams and ALL FAIL today (there is no TaskTree component;
the detail page lists DIRECT children only; the board badge counts DIRECT
children):

  * a TaskTree component exists and renders RECURSIVELY (a node renders itself
    for its children — any depth) with an expand/collapse chevron per parent.
  * TaskDetailPage imports TaskTree, renders a SUBTREE done roll-up header
    (descendant_done / descendant_count), and drops the old flat
    "Child tasks (X/Y done)" one-level list.
  * TasksPage's corner badge counts the whole DESCENDANT subtree (not just
    direct children) with an accurate "descendant task(s)" tooltip.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_TREE = _SRC / "components" / "tasks" / "TaskTree.tsx"
_DETAIL = _SRC / "pages" / "TaskDetailPage.tsx"
_TASKS = _SRC / "pages" / "TasksPage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── TaskTree component — recursive + collapsible ───────────────────────

def test_task_tree_component_exists_and_exports():
    assert _TREE.exists(), "web/src/components/tasks/TaskTree.tsx must exist"
    src = _read(_TREE)
    assert "TaskTree" in src, "the component must be named/exported TaskTree"
    assert "export" in src


def test_task_tree_is_recursive_to_any_depth():
    src = _read(_TREE)
    # A recursive node component renders ITSELF for each child. The node name
    # must appear both as a definition AND as a self-reference in its own JSX.
    assert "TaskTreeNode" in src, \
        "an inner recursive node component (TaskTreeNode) is expected"
    assert src.count("TaskTreeNode") >= 2, \
        "TaskTreeNode must render itself recursively for children (any depth)"
    assert "children" in src, "the node must render its children collection"


def test_task_tree_has_collapsible_chevron_and_checkbox():
    src = _read(_TREE)
    assert "useState" in src, "expand/collapse state must be local per node"
    # A rotating chevron toggle (unicode caret or an aria/expand affordance).
    assert ("▸" in src or "▾" in src or "chevron" in src.lower()
            or "expand" in src.lower()), "each parent node needs an expand/collapse chevron"
    # Reuse the existing Checkbox + Hermes status tones (no invented palette).
    assert "Checkbox" in src, "reuse the shared done Checkbox"
    assert "var(--accent-" in src, "status tones must use Hermes accent tokens"


def test_task_tree_nodes_are_navigable():
    src = _read(_TREE)
    # Clicking a node still opens its detail (navigate to /tasks/:id).
    assert "/tasks/" in src or "navigate" in src, \
        "a tree node must remain clickable through to its detail page"


# ── TaskDetailPage — subtree tree + roll-up header ─────────────────────

def test_detail_page_renders_task_tree_and_subtree_rollup():
    src = _read(_DETAIL)
    assert "TaskTree" in src, "TaskDetailPage must render the recursive TaskTree"
    assert "descendant_count" in src and "descendant_done" in src, \
        "the section header must show the SUBTREE done roll-up " \
        "(descendant_done / descendant_count)"


def test_detail_page_drops_flat_one_level_child_list():
    src = _read(_DETAIL)
    # The old flat list header was 'Child tasks (X/Y done)' — a one-level view.
    assert "Child tasks (" not in src, \
        "the flat one-level 'Child tasks (X/Y done)' list must be replaced by " \
        "the recursive subtree tree"


# ── TasksPage — board badge counts the whole descendant subtree ────────

def test_board_badge_counts_descendant_subtree():
    src = _read(_TASKS)
    assert "descendant" in src.lower(), \
        "the board badge must reflect the whole descendant subtree"
    assert "descendantCount" in src, \
        "a descendantCount map (whole-subtree) must back the corner badge"
    assert "descendant task" in src, \
        "the badge tooltip must read 'N descendant task(s)'"
