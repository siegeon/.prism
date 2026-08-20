"""The gate decision panel's check row dead-ends on an unshipped receipt —
task 8a06e121.

The PRISM SPA has NO JS test runner, so UI-first acceptance criteria are
pinned by asserting the ACTUAL web source (TSX) — the same convention as
tests/unit/test_task_oracle_always_visible_ui.py.

THE DEFECT: TaskDetailPage.tsx's own `GateReadiness` type (~line 120) never
declares `unshipped`/`ship_on_approve` — fields GET /api/conductor/gate/
readiness already returns (api/conductor.py, and LiveGatePanel.tsx's OWN
copy of this type already has them). So when readiness reports a genuinely
PASSING oracle receipt that is merely unshipped, the panel's evidence-table
check row (~line 1983-2003) falls into its generic `receipt_ok === false`
branch and renders the receipt as "stale / failed" (or "missing") with a
"↻ re-run" button — both false and a dead end: re-running the oracle mints
another PASSING receipt (the evidence was never the problem), so the human
clicks re-run, watches it "pass" again, and Approve still refuses. This is
the exact "Never show a human an Approve that dead-ends" shape this task
exists to close, reopened at the check-row pill instead of the Approve
button.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parent.parent.parent  # services/prism-service
_TDP = _ROOT / "prism_service" / "web" / "src" / "pages" / "TaskDetailPage.tsx"


def _read(p: Path) -> str:
    assert p.is_file(), f"missing source file: {p}"
    return p.read_text(encoding="utf-8")


def _window(src: str, marker: str, before: int = 200, after: int = 1200) -> str:
    i = src.find(marker)
    assert i >= 0, f"marker not found in source: {marker!r}"
    return src[max(0, i - before): i + after]


def test_gate_readiness_type_carries_the_unshipped_signal() -> None:
    """LiveGatePanel.tsx already declares `unshipped?: boolean` on its own
    copy of this response shape (components/live/LiveGatePanel.tsx:23) — the
    in-page decision panel's type must not silently drop the same field the
    server already sends it."""
    chunk = _window(_read(_TDP), "type GateReadiness", after=1400)
    assert re.search(r"\bunshipped\??\s*:\s*boolean", chunk), (
        "GateReadiness must declare `unshipped?: boolean` so the panel can "
        "tell a genuinely-failing receipt apart from a passing-but-unshipped "
        "one")


def test_check_row_does_not_call_an_unshipped_receipt_stale_or_missing() -> None:
    """The evidence-table check row must special-case `unshipped` BEFORE
    falling into the generic stale/failed/missing branch — a passing receipt
    that is merely unshipped is not stale, not failed, and not missing."""
    chunk = _window(_read(_TDP), 'oracle receipt · {gateReadiness?.receipt?.adapter',
                    before=200, after=2200)
    assert "gateReadiness?.unshipped" in chunk, (
        "the check-row pill must branch on the live `unshipped` signal, not "
        "just on the bare receipt_ok boolean")


def test_re_run_is_not_offered_for_an_unshipped_receipt() -> None:
    """Re-running the oracle cannot fix an unshipped receipt (it mints
    another PASSING receipt) — offering `mintEvidence` there is the dead end
    this task exists to remove, the same reasoning api/conductor.py's
    red_gate branch already uses ('re-running the oracle cannot help')."""
    chunk = _window(_read(_TDP), 'oracle receipt · {gateReadiness?.receipt?.adapter',
                    before=200, after=2200)
    m = re.search(r"gateReadiness\?\.unshipped[\s\S]{0,400}", chunk)
    assert m, "no unshipped branch found in the check row to inspect"
    assert "mintEvidence" not in m.group(0), (
        "the unshipped branch must not offer the re-run action — re-running "
        "cannot fix a shipped-ness problem")
