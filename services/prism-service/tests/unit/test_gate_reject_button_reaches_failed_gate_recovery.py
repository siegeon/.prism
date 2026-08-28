"""UI contract test: the Reject button reaches the 7.13.134 backend recovery
path (task 12029f92).

The PRISM SPA has NO JS test runner, so this is pinned by asserting the
ACTUAL TSX source -- the same pattern as
tests/unit/test_step_rail_uses_task_own_workflow.py.

BUG: the button unmounted entirely once task.gate_state === "failed", so a
real user could never reach ConductorService._reject_gate's override-based
recovery (7.13.134) -- it was backend-only.

FIX: the button stays mounted when gate_state === "failed" AND the existing
gateOverride checkbox is ticked, is disabled on a failed gate until override
is ticked, and relabels to "Reject (redo)" on a failed gate -- mirroring the
neighboring Approve button's existing "Approve (recover)" pattern.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_TASK_DETAIL_PAGE = (
    _HERE.parent.parent.parent
    / "prism_service"
    / "web"
    / "src"
    / "pages"
    / "TaskDetailPage.tsx"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_reject_button_stays_mounted_on_a_failed_gate_only_with_override():
    src = _read(_TASK_DETAIL_PAGE)

    assert '(task.gate_state !== "failed" || gateOverride) && (' in src, \
        "the Reject button's render guard must stay mounted on a failed " \
        "gate when gateOverride is ticked, not unmount unconditionally"

    assert (
        'disabled={busy || !gateReason.trim() '
        '|| (task.gate_state === "failed" && !gateOverride)}'
    ) in src, \
        "the Reject button must additionally disable on a failed gate " \
        "until gateOverride is ticked"

    assert '"Reject (redo)" : "Reject"' in src, \
        "the Reject button must relabel to Reject (redo) on a failed gate, " \
        "mirroring Approve's Approve (recover) pattern"
