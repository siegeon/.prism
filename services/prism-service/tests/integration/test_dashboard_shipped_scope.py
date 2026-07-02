"""RED scaffold — Dashboard shipped-count scope label (task b5f2c78e).

STRESS-LOOP FINDING: the Dashboard shows "TASKS SHIPPED 23" (and Delivery
flow "23 SHIPPED"), counting EVERY task with a completed_at (roots AND
children), while the /tasks board shows "COMPLETED → 17" (roots only, by
the done-tasks-off-board product decision). The two surfaces disagree by
the 6 completed child subtasks and read as a contradiction for the same
"completed/shipped" concept.

Fix: the dashboard's shipped metric keeps its all-done scope (roots +
children) but LABELS that scope explicitly ("incl. subtasks"), so it no
longer reads as the same number the roots-only board reports.

Source-structure test — scans the real DashboardPage render path on disk.
FAILS today: the shipped metrics are labelled bare "Tasks shipped" /
"shipped" with no scope marker.
"""
from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_DASH = _SERVICE_ROOT / "prism_service" / "web" / "src" / "pages" / "DashboardPage.tsx"


def test_shipped_kpi_declares_subtask_scope():
    """The trend KPI over flow.completed carries an explicit scope marker."""
    src = _DASH.read_text(encoding="utf-8")
    assert "incl. subtasks" in src, (
        "the dashboard shipped/completed metrics must declare their scope "
        "(they count roots + children) so they don't contradict the "
        "roots-only /tasks board 'Completed' count"
    )


def test_bare_tasks_shipped_label_is_scoped():
    """The bare 'Tasks shipped' label (which read as == board Completed) is
    replaced by a scoped label."""
    src = _DASH.read_text(encoding="utf-8")
    assert 'label="Tasks shipped"' not in src, (
        "bare 'Tasks shipped' contradicts the roots-only board count — "
        "scope it to 'Tasks shipped (incl. subtasks)'"
    )
    assert 'label="shipped"' not in src, (
        "bare Delivery-flow 'shipped' stat must be scoped too"
    )
