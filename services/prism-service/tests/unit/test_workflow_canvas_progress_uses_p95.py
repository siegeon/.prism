"""The Workflows canvas active-node fill bar paces off p95 of a step's
real recent durations, not a plain mean (owner 2026-08-26, watching the
bar live: "it should be the p95 duration of the last x runs (like the
count of the pills or something)").

Root cause fixed: WorkflowsPage.tsx's frame callback paced the bar via
`elapsedSeconds / step.average_duration_seconds` -- a plain mean sourced
from an external workflow engine, dragged around by one outlier so the
bar either rocketed to 98% early or crawled long past when the step
usually finishes. p95StepDurationSeconds computes p95 from the SAME
run-history dataset already driving the pill rail (visibleRunHistory,
capped at RUN_RAIL_PILLS), reusing each WorkflowRun's real per-step
timeline (startedAt/endedAt) -- no separate fetch, no server change.

The PRISM SPA has NO JS test runner, so UI behavior is pinned by
asserting the ACTUAL TSX source (tests/unit/test_conductor_page_
animated_cleanup_ui.py:4-6).
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_TSX = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
       / "WorkflowsPage.tsx")


def _src() -> str:
    return _TSX.read_text(encoding="utf-8")


def test_p95_helper_exists_and_computes_a_real_percentile():
    src = _src()
    assert "function p95StepDurationSeconds(" in src
    fn_start = src.index("function p95StepDurationSeconds(")
    fn_body = src[fn_start:fn_start + 900]
    # A real p95 sorts the samples and indexes near the top, rather than
    # just reporting .length or an unsorted max -- pins against a
    # regression that renames the function but keeps a fake computation.
    assert ".sort(" in fn_body
    assert "0.95" in fn_body
    # Below-floor guard: too few same-step samples must not pace anything.
    assert "samples.length < 3" in fn_body
    assert "return null" in fn_body


def test_p95_reads_from_the_same_dataset_the_pill_rail_counts():
    src = _src()
    # visibleRunHistory is the exact array the pill rail (RUN_RAIL_PILLS)
    # renders -- the fix must reuse it, never a separately-fetched set.
    assert "p95StepDurationSeconds(visibleRunHistory, runtime.currentStep)" in src


def test_frame_pacing_prefers_p95_over_the_plain_mean():
    src = _src()
    frame_at = src.index("const frame = (now: number) => {")
    # The old direct assignment must be gone from the pacing block.
    old_block_start = src.index("if (runtime?.status === \"running\"", frame_at)
    old_block = src[old_block_start:old_block_start + 900]
    assert "const average = step?.average_duration_seconds;" not in old_block
    assert "const p95 = p95StepDurationSeconds(" in old_block
    assert "const pacing = p95 ?? step?.average_duration_seconds;" in old_block
    assert "elapsedSeconds / pacing" in old_block


def test_frame_effect_depends_on_run_history_so_p95_stays_fresh():
    src = _src()
    assert (
        "}, [selectedNodeId, selectedWorkflow, workflowRun, testStep, "
        "replayStoppedAt, workflowRunHistory]);"
    ) in src
