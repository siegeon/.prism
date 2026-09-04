"""Failing UI contract tests for task ce471e06 ("The flow view shows the
driven path and real step time").

The PRISM SPA has NO JS test runner, so UI acceptance criteria are pinned by
asserting the ACTUAL web source (TSX/TS) — the same pattern as
tests/unit/test_conductor_page_animated_cleanup_ui.py.

Every assertion runs against COMMENT-STRIPPED source, so a comment can never
satisfy it (WorkflowsPage.tsx line ~1061 already mentions flow_report_failure
in a comment today — that must not count).

RED at base 927d1e42, measured 2026-09-04 in this worktree:
- neither `traversedPath` nor `runMode` exists in WorkflowsPage.tsx or
  live/workflowGraph.ts;
- `task_motion_s` is not bound anywhere in WorkflowsPage.tsx (the owner's
  screen showed a negative step timer, `RUN 0s / -4s`, against 25 minutes of
  real motion);
- `flow_report_failure` appears in WorkflowsPage.tsx only inside a comment;
- three `Math.min(0.98, ...)` cap sites exist (lines ~1453/~1500/~1537); the
  two live-progress sites mask overrun as a near-full bar. Only the replay
  pacing site (~1537) is allowed to keep the cap.

They go green only when plan steps 1-6 land: run-mode dim behind a stats
toggle, the traversed-path highlight from the run's own advance_task rows,
the active-node label bound to activity.task_motion_s (never heartbeat
age_s), the flow_report_failure-derived attempt badge, and an explicit
OVERRUN state replacing the two live-progress caps.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "prism_service" / "web" / "src"
PAGE = WEB / "pages" / "WorkflowsPage.tsx"
GRAPH = WEB / "live" / "workflowGraph.ts"


def _code(path: Path) -> str:
    """The file's source with /* */ blocks and // line comments removed,
    so an assertion can only be satisfied by rendered/executed code."""
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    kept = []
    for line in src.splitlines():
        # Drop // comments; keep '://' so string URLs survive.
        kept.append(re.sub(r"(?<!:)//.*$", "", line))
    return "\n".join(kept)


def _near(code: str, first: str, second: str, window: int = 240) -> bool:
    """True when the two patterns occur within `window` chars of each other,
    in either order — a proximity check on real code, not comments."""
    return bool(
        re.search(first + r"[\s\S]{0," + str(window) + r"}" + second, code)
        or re.search(second + r"[\s\S]{0," + str(window) + r"}" + first, code)
    )


def test_the_selected_task_path_is_highlighted():
    """AC-1 + AC-4/AC-5 source clauses: the rail highlights the selected
    task's own traversed path, and run mode dims (never removes) catalog
    stats behind a toggle."""
    page = _code(PAGE)
    graph = _code(GRAPH)

    # The page still attaches on the ?task= param (the existing seam the
    # run mode keys off).
    assert 'searchParams.get("task")' in page, (
        "WorkflowsPage.tsx lost the ?task= attach seam the run view keys off"
    )

    # A traversedPath render option flows from the page into workflowGraph,
    # derived from the run's own advance_task history rows.
    assert "traversedPath" in page, (
        "WorkflowsPage.tsx does not build a traversedPath for the selected "
        "task — the rail cannot light the path behind the piece"
    )
    assert "traversedPath" in graph, (
        "workflowGraph.ts has no traversedPath render option — the wires "
        "along the driven path are not stroked lit"
    )
    assert "advance_task" in page, (
        "WorkflowsPage.tsx never reads the run's advance_task history rows, "
        "so the traversed path cannot come from THIS run"
    )

    # Run mode exists on both sides, and in the graph it routes catalog
    # stats through the existing dim draw path — dimmed, never removed.
    assert "runMode" in page and "runMode" in graph, (
        "no runMode render option exists — the run view cannot foreground "
        "this run's trace over catalog-wide stats"
    )
    assert _near(graph, r"\brunMode\b", r"\bdim\b"), (
        "workflowGraph.ts does not condition its dim draw path on runMode — "
        "catalog stats (PASSED chips, cost lines) stay at full strength in "
        "the single-run view, or were removed instead of dimmed"
    )

    # A visible stats toggle restores the catalog stats without leaving the
    # run (AC-5).
    assert _near(page, r"\brunMode\b", r"(?i)stats"), (
        "WorkflowsPage.tsx has no stats toggle seam tied to run mode — the "
        "folded catalog stats cannot be restored without leaving the run"
    )


def test_the_active_step_shows_step_elapsed_and_attempts():
    """AC-2 + AC-3 + AC-7: the active node's time label binds THIS run's
    task_motion_s (never heartbeat age), a failed dispatch renders an
    attempt count from flow_report_failure rows, and done > total renders
    an explicit OVERRUN state instead of a capped near-full bar."""
    page = _code(PAGE)
    graph = _code(GRAPH)

    # AC-2: the step-elapsed binding exists...
    assert "task_motion_s" in page, (
        "WorkflowsPage.tsx never binds activity.task_motion_s — the active "
        "node shows heartbeat age (the owner saw 'RUN 0s / -4s' against 25 "
        "minutes of real motion)"
    )
    # ...and no line that binds it also binds heartbeat age. Per-line check
    # of the enclosing expression, so a comment above cannot shift the guard.
    for line in page.splitlines():
        if "task_motion_s" in line:
            assert "age_s" not in line and "heartbeat" not in line, (
                "the step-elapsed expression mixes task_motion_s with "
                "heartbeat age — the label would still read as seconds-old "
                "signal, the exact misfire the ticket names: %r" % line
            )

    # AC-3: the attempt counter is computed from real setback rows and
    # rendered near an attempt label.
    assert "flow_report_failure" in page, (
        "flow_report_failure appears in WorkflowsPage.tsx only as prose (a "
        "comment) — no code counts the failed dispatches, so two retries "
        "stay invisible on screen"
    )
    assert "advance_refused" in page, (
        "the setback counter ignores advance_refused rows — a refused "
        "advance would not raise the attempt count"
    )
    assert _near(page, r"flow_report_failure", r"(?i)attempt", window=2000), (
        "no attempt label is rendered from the flow_report_failure-derived "
        "counter — the count is computed nowhere or shown nowhere"
    )

    # AC-7: an explicit overrun state exists, and the two live-progress
    # Math.min(0.98, ...) caps no longer mask done > total. Only the replay
    # pacing site may keep the cap.
    assert re.search(r"(?i)overrun", page) or re.search(r"(?i)overrun", graph), (
        "no OVERRUN state is rendered anywhere — a wedged step still paints "
        "as an honest near-full bar"
    )
    caps = page.count("Math.min(0.98")
    assert caps <= 1, (
        "WorkflowsPage.tsx still has %d Math.min(0.98, ...) cap sites; the "
        "two live-progress sites (~1453/~1500) must become an explicit "
        "overrun branch, leaving at most the replay pacing cap" % caps
    )
