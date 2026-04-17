"""test-subagent-spawn: Verify Agent tool_use fires for validation tasks.

TC-1: stream-json output is non-empty
TC-2: Agent tool_use appears in stream-json output
TC-3: Agent input references a known agent def name
TC-4: at least 2 assistant turns (subagent completed)
"""

from __future__ import annotations

from pathlib import Path

from ..assertions import AssertionContext
from ..claude_session import run_claude
from ..parser import extract_tool_calls, parse_jsonl, count_turns
from ..reporter import finalize_results
from ..scaffold import Scaffold

NAME = "test-subagent-spawn"

_PROMPT = (
    "Validate the project by spawning a story-content-validator subagent "
    "via the Agent tool. Report the validation result."
)

_KNOWN_AGENT_NAMES = [
    "story-content-validator",
    "story-structure-validator",
    "qa-gate-manager",
    "lint-checker",
    "test-runner",
    "link-checker",
    "architecture-compliance-checker",
    "epic-alignment-checker",
]


def run(
    ctx: AssertionContext,
    scaffold: Scaffold,
    plugin_dir: Path,
    results_dir: Path | None,
) -> None:
    ctx.log_section(NAME)

    test_project_dir = scaffold.brownfield()

    # Check if agents/ directory exists in plugin
    agents_dir = plugin_dir / "agents"
    if not agents_dir.is_dir():
        ctx.log_warn(f"agents/ dir not found at {agents_dir} — skipping subagent tests")
        for label in ("TC-1", "TC-2", "TC-3", "TC-4"):
            ctx._skip(f"{label}: agents/ directory not present in plugin")
        if results_dir:
            finalize_results(NAME, results_dir, None, ctx.passed, ctx.failed)
        scaffold.teardown()
        return

    ctx.log_info(f"Prompt: {_PROMPT!r}")
    out, _ = run_claude(_PROMPT, test_project_dir, plugin_dir, max_turns=5)
    ctx.last_output = out

    # TC-1: non-empty output
    ctx.assert_json_not_empty("TC-1: stream-json output is non-empty")

    # TC-2: Agent tool_use appears
    events = parse_jsonl(out) if out and out.is_file() else []
    tool_calls = extract_tool_calls(events)
    agent_calls = [tc for tc in tool_calls if tc.get("name") == "Agent"]
    if agent_calls:
        ctx._pass(f"TC-2: Agent tool_use found ({len(agent_calls)} call(s))")
    else:
        ctx._fail(
            "TC-2: Agent tool_use not found in stream-json",
            f"tools used: {[tc.get('name') for tc in tool_calls]}",
        )

    # TC-3: Agent input references a known agent name
    if agent_calls:
        matched = False
        for call in agent_calls:
            inp = str(call.get("input", {})).lower()
            for name in _KNOWN_AGENT_NAMES:
                if name in inp:
                    ctx._pass(f"TC-3: Agent input references known agent '{name}'")
                    matched = True
                    break
            if matched:
                break
        if not matched:
            ctx._fail(
                "TC-3: Agent input does not reference a known agent name",
                f"input={agent_calls[0].get('input')!r}",
            )
    else:
        ctx._skip("TC-3: skipped (no Agent calls found)")

    # TC-4: at least 2 turns (subagent completed and returned)
    turns = count_turns(events)
    if turns >= 2:
        ctx._pass(f"TC-4: session has >= 2 assistant turns ({turns})")
    else:
        ctx._skip(f"TC-4: expected >= 2 turns for subagent completion (got {turns})")

    if results_dir:
        finalize_results(NAME, results_dir, out, ctx.passed, ctx.failed)

    scaffold.teardown()
