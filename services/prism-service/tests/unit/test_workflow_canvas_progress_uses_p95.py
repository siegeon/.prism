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


def _running_block(src: str) -> str:
    """The `if (runtime?.status === "running" ...) { ... }` body, parsed by
    BRACE DEPTH.

    SUPERSEDES the fixed character window this case used to slice. That
    window was widened once already (900 -> 2000 chars, 2026-08-26) when a
    comment block pushed the pacing lines out of range, and it broke a third
    time on 2026-08-29 when the sawtooth-removal comment did the same. A
    window measures how much PROSE sits above a line, which is not what this
    test is about; brace depth measures the block itself.
    """
    frame_at = src.index("const frame = (now: number) => {")
    start = src.index("if (runtime?.status === \"running\"", frame_at)
    open_at = src.index("{", start)
    depth, i = 0, open_at
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError("unbalanced running block")


def test_frame_pacing_prefers_p95_over_the_plain_mean():
    src = _src()
    old_block = _running_block(src)
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
    #
    # SUPERSEDED AGAIN (2026-08-29, per-node verdicts on a drilled layer):
    # `nodeVerdicts` joined the same deps list, so the frame effect re-runs
    # when a behaviour layer's real node states arrive. The INVARIANT this
    # test exists for is that the pacing inputs stay in the deps -- pinning
    # the whole list as one literal string made every later addition read as
    # a regression, so each required dep is asserted on its own now.
    src = _src()
    start = src.index("}, [selectedNodeId, selectedWorkflow, workflowRun,")
    deps = src[start:src.index("]", start)]
    for required in ("workflowRunHistory", "workflows", "testStep",
                     "replayStoppedAt", "selectedWorkflow", "workflowRun"):
        assert required in deps, (
            f"{required} left the frame effect deps, so pacing runs on a "
            "stale closure")


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
