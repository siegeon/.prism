"""A children-based percentage is shown as itself, not blended with steps.

Owner 2026-08-06, looking at the epic card: "9/11 ?!? it says 7/13 still".

The card renders "IMPLEMENT TASKS · 77.62% · 7/13". Those are the SAME
measure, rendered twice, one of them mangled. Live server payload for that
epic:  basis="children", pct=0.538462, children 7/13  — and 7/13 is exactly
0.538462. The epic is 53.85% done.

SdlcProgress then computes  overallSeed = (curIdx + pct) / steps.length
= (8 + 0.538462) / 11 = 0.77622, i.e. it treats a WHOLE-EPIC fraction as if
it were "how far through the current step we are" and folds it into a
step-position measure. The step blend is right for basis="time"/"fanout",
where pct really does describe the current step. For basis="children" it
produces a number that describes nothing, sitting next to the 7/13 that
contradicts it.

Pinned here as a source contract because the PRISM SPA has no JS test
runner (the convention documented in
tests/unit/test_conductor_page_animated_cleanup_ui.py).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SRC = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "components"
        / "conductor" / "SdlcProgress.tsx")


def _overall_seed_definition(src: str) -> str:
    """The `const overallSeed = ...` expression, up to its terminating
    semicolon. Parsed rather than substring-matched: `basis === "children"`
    already appears elsewhere in this file (deciding whether to append the
    N/M suffix), so a bare `in src` check passes vacuously against code that
    never touches the percentage. That is the recorded 'a comment/unrelated
    occurrence satisfies the assertion' trap."""
    i = src.index("const overallSeed")
    return src[i:src.index(";", i) + 1]


def test_a_children_basis_percentage_is_rendered_as_itself():
    """When the server says basis='children', pct IS the epic's progress
    (7/13 = 0.538462) and must be shown unmodified, so it agrees with the
    N/M printed beside it instead of contradicting it."""
    src = _SRC.read_text(encoding="utf-8")
    defn = _overall_seed_definition(src)
    assert 'basis === "children"' in defn, (
        "overallSeed blends a whole-epic children fraction into a step "
        f"position, printing 77.62% next to 7/13. Definition was: {defn}")


def test_the_blend_is_still_used_for_step_scoped_bases():
    """The step blend stays where pct really does describe the current step
    (basis='time'/'fanout') - do not throw it away."""
    src = _SRC.read_text(encoding="utf-8")
    defn = _overall_seed_definition(src)
    assert "curIdx" in defn and "steps.length" in defn, (
        f"the step-position blend disappeared entirely: {defn}")
