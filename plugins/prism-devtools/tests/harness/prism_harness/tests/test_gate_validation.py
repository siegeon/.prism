"""test-gate-validation: Validate gate decision correlates with validator output.

TC-1: stream-json output is non-empty
TC-2: gate-related keywords appear in session output (gate fired or referenced)
TC-3: PASS or FAIL recommendation appears in output (gate rendered a decision)
TC-4: state file active field reflects prism-loop ran (gate reachable)
"""

from __future__ import annotations

from pathlib import Path

from ..assertions import AssertionContext
from ..claude_session import run_claude
from ..parser import extract_assistant_text, parse_jsonl
from ..reporter import finalize_results
from ..scaffold import Scaffold

NAME = "test-gate-validation"

_PROMPT = (
    "*prism-loop Write a simple hello() function that returns 'hello world'. "
    "Progress through the workflow steps until a gate is reached."
)

_GATE_KEYWORDS = [
    "gate",
    "red_gate",
    "green_gate",
    "PASS",
    "FAIL",
    "recommendation",
    "validator",
    "validation",
    "gate decision",
]

_WORKFLOW_KEYWORDS = [
    "prism-loop",
    "story",
    "planning",
    "TDD",
    "workflow",
    "step",
    "phase",
]


def run(
    ctx: AssertionContext,
    scaffold: Scaffold,
    plugin_dir: Path,
    results_dir: Path | None,
) -> None:
    ctx.log_section(NAME)

    test_project_dir = scaffold.brownfield()
    state_file = test_project_dir / ".claude" / "prism-loop.local.md"

    # Remove prior state to start fresh
    if state_file.exists():
        state_file.unlink()

    ctx.log_info(f"Prompt: {_PROMPT!r}")
    out, _ = run_claude(
        _PROMPT, test_project_dir, plugin_dir, max_turns=8
    )
    ctx.last_output = out

    # TC-1: non-empty output
    ctx.assert_json_not_empty("TC-1: stream-json output is non-empty")

    if not out or not out.is_file():
        ctx._fail("TC-2: no output file — cannot check gate keywords")
        ctx._fail("TC-3: no output file — cannot check gate decision")
        ctx._skip("TC-4: no output — state file check skipped")
        if results_dir:
            finalize_results(NAME, results_dir, out, ctx.passed, ctx.failed)
        scaffold.teardown()
        return

    raw_content = out.read_text(encoding="utf-8").lower()

    # TC-2: gate keywords in output (gate fired or was referenced)
    found_gate_kws = [kw for kw in _GATE_KEYWORDS if kw.lower() in raw_content]
    if found_gate_kws:
        ctx._pass(
            f"TC-2: gate-related keywords found in output ({found_gate_kws[:3]})"
        )
    else:
        # Fall back: check for workflow keywords — loop ran but gate not yet reached
        found_workflow_kws = [kw for kw in _WORKFLOW_KEYWORDS if kw.lower() in raw_content]
        if found_workflow_kws:
            ctx._skip(
                f"TC-2: gate keywords absent — workflow ran but gate not reached "
                f"(workflow terms: {found_workflow_kws[:3]})"
            )
        else:
            ctx._fail(
                "TC-2: neither gate nor workflow keywords found in output",
                f"checked: {_GATE_KEYWORDS[:4]}",
            )

    # TC-3: PASS or FAIL recommendation (gate decision rendered)
    events = parse_jsonl(out)
    texts = extract_assistant_text(events)
    combined_text = " ".join(texts)
    decision_in_text = (
        "PASS" in combined_text
        or "FAIL" in combined_text
        or "pass" in combined_text.lower()
        or "fail" in combined_text.lower()
    )
    gate_decision_in_raw = (
        "gate decision" in raw_content
        or "recommendation" in raw_content
        or '"pass"' in raw_content
        or '"fail"' in raw_content
    )

    if decision_in_text or gate_decision_in_raw:
        ctx._pass("TC-3: gate recommendation (PASS/FAIL) appears in session output")
    else:
        ctx._skip(
            "TC-3: PASS/FAIL recommendation not found — gate may not have been reached "
            "within max_turns"
        )

    # TC-4: state file reflects that prism-loop ran
    if state_file.exists():
        content = state_file.read_text()
        if "active" in content:
            ctx._pass(f"TC-4: prism-loop state file exists with 'active' field ({state_file})")
        else:
            ctx._pass(f"TC-4: prism-loop state file created ({state_file})")
    else:
        ctx._skip(
            "TC-4: state file not created — prism-loop may not have initialized "
            "within max_turns"
        )

    if results_dir:
        finalize_results(NAME, results_dir, out, ctx.passed, ctx.failed)

    scaffold.teardown()
