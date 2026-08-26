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
    # The old direct assignment must be gone from the pacing block. Window
    # widened from 900 -> 2000 chars (2026-08-26 follow-up to the p95 fix above):
    # the catalog-fallback comment block pushed the pacing lines further
    # from the block's start; 900 no longer reaches them.
    old_block_start = src.index("if (runtime?.status === \"running\"", frame_at)
    old_block = src[old_block_start:old_block_start + 2000]
    assert "const average = step?.average_duration_seconds;" not in old_block
    assert "const p95 = p95StepDurationSeconds(" in old_block
    assert "const pacing = p95 ?? step?.average_duration_seconds;" in old_block
    assert "elapsedSeconds / pacing" in old_block


def test_frame_effect_depends_on_run_history_and_catalog_so_pacing_stays_fresh():
    # SUPERSEDES the prior exact-string check (task <linked-node-progress
    # fix>): `workflows` (the full connected catalog) joined the deps list
    # alongside workflowRunHistory so a linked child step's catalog-wide
    # fallback lookup (see test_linked_child_step_falls_back_to_the_full_
    # catalog below) doesn't run on a stale closure.
    src = _src()
    assert (
        "}, [selectedNodeId, selectedWorkflow, workflowRun, testStep, "
        "replayStoppedAt, workflowRunHistory, workflows]);"
    ) in src


def test_linked_child_step_falls_back_to_the_full_catalog():
    """A linked CHILD node (e.g. verify_green_state's "Build and test",
    whose own steps "build"/"test" never appear in selectedWorkflow.steps,
    the CONDUCTOR's own 10 steps) must not silently fall through to the
    indeterminate wiggle just because the direct lookup misses -- it
    should search the full connected catalog (`workflows` state) too.
    Owner 2026-08-26, live screenshot: "Build and test" stuck cycling at
    ~23% fill after 4m51s of real elapsed time, because BOTH the p95 AND
    average lookups were scoped to the wrong workflow's step list."""
    src = _src()
    assert (
        "const step = selectedWorkflow.steps.find((candidate) => "
        "candidate.id === runtime.currentStep)\n          ?? "
        "workflows.flatMap((wf) => wf.steps).find((candidate) => "
        "candidate.id === runtime.currentStep);"
    ) in src
