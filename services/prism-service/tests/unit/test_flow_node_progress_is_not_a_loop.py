"""A flow node must not animate progress it is not making.

Owner, 2026-08-29, watching a task parked at plan_gate on the Flow view:
"the progress on plan gate has filled to full like 15 times - clearly not
fixing the issue", and "its not a progress its just a animation loop ... we
can math to full but it should never fill up and then cycle to start again,
unless the sub tasks in that flow step are actually getting done."

THE MECHANISM was WorkflowsPage.tsx:

    progress: pacing && pacing > 0
      ? Math.min(0.98, elapsedSeconds / pacing)
      : 0.12 + ((elapsedSeconds % 18) / 18) * 0.68

For a step with no duration history, that second branch is a pure wall-clock
sawtooth: it sweeps 12% -> 80% and snaps back every 18 SECONDS, forever, no
matter what the step is doing. ~15 cycles in the ~4.5 minutes the owner
watched. The same wiggle produced the earlier "Build and test stuck cycling
at ~23% after 4m51s" report already recorded in that file's comments.

The determinate branch is fine and stays: with real pacing the bar maths to
full ONCE and stops at 0.98.

The PRISM SPA has no JS test runner, so this pins the actual sources.
"""
from __future__ import annotations

import re
from pathlib import Path

_WEB = (Path(__file__).resolve().parent.parent.parent / "prism_service/web/src")
_PAGE = _WEB / "pages/WorkflowsPage.tsx"
_GRAPH = _WEB / "live/workflowGraph.ts"


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def test_no_wall_clock_sawtooth_drives_node_progress():
    """The literal loop, gone from CODE (a comment describing it is fine)."""
    code = _strip_comments(_PAGE.read_text(encoding="utf-8"))
    assert "% 18" not in code, (
        "the 18-second sawtooth still drives node progress: it fills and "
        "resets forever while the step does nothing")
    assert not re.search(r"elapsedSeconds\s*%", code), (
        "node progress is still a modulo of wall-clock elapsed time")


def test_a_step_without_pacing_claims_no_progress():
    code = _strip_comments(_PAGE.read_text(encoding="utf-8"))
    # Task ce471e06 (AC-7) moved the ternary inside the cap call, so the
    # shape is now `Math.min(1, pacing && pacing > 0 ? ... : 0)`. The
    # invariant is untouched and still pinned: with no pacing the fallback
    # branch is a literal 0, never a wall-clock wiggle.
    m = re.search(r"pacing\s*&&\s*pacing\s*>\s*0\s*\?(.*?)\),\s*\n?\s*indeterminate",
                  code, flags=re.DOTALL)
    assert m, "the progress ternary was not found"
    branches = m.group(1)
    fallback = branches.split(":")[-1].strip()
    assert fallback == "0", (
        f"a step with no duration history still claims progress: {fallback!r}")


def test_the_determinate_bar_still_maths_to_full():
    """Owner: 'we can math to full'. The real ETA bar must survive."""
    code = _strip_comments(_PAGE.read_text(encoding="utf-8"))
    # Superseded by task ce471e06 (AC-7). The owner's "we can math to full"
    # is now literal: the 0.98 cap that stopped just short is gone, replaced
    # by Math.min(1, ...) plus an explicit OVERRUN state once elapsed passes
    # pacing. The pacing-based fill itself must still be here.
    assert "Math.min(1, pacing && pacing > 0 ? elapsedSeconds / pacing : 0)" in code, (
        "the honest pacing-based fill was removed along with the loop")
    assert "Math.min(0.98, elapsedSeconds / pacing)" not in code, (
        "the 0.98 cap is back; a step past its pacing must read OVERRUN, "
        "not park at a near-full bar")


def test_an_indeterminate_node_paints_no_body_fill():
    """A painted width IS a claim about how far along the step is."""
    code = _strip_comments(_GRAPH.read_text(encoding="utf-8"))
    i = code.index("const bodyY")
    tail = code[i:i + 700]
    assert "if (!active.indeterminate)" in tail, (
        "the node body fill is still painted for an indeterminate step")
    guard = tail.index("if (!active.indeterminate)")
    body = tail[guard:]
    # both the fill and its leading-edge marker are position claims
    assert body.count("fillRect") >= 2, (
        "the leading-edge marker escaped the indeterminate guard; it is a "
        "position claim too")
