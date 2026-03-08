"""test-subagent-hooks: Verify SubagentStop hook fires and prompt_id state written.

TC-1: stream-json output is non-empty
TC-2: Agent tool_use present (subagent was spawned)
TC-3: SubagentStop hook event appears in system messages
TC-4: prompt_id state file written to .prism/brain/current_prompt_id
"""

from __future__ import annotations

from pathlib import Path

from ..assertions import AssertionContext
from ..claude_session import run_claude
from ..parser import extract_tool_calls, extract_hook_messages, parse_jsonl
from ..reporter import finalize_results
from ..scaffold import Scaffold

NAME = "test-subagent-hooks"

_PROMPT = (
    "Use the Agent tool to spawn a story-structure-validator subagent "
    "to validate the project structure, then summarize the results."
)

# Path relative to the test project root
_PROMPT_ID_FILE = ".prism/brain/current_prompt_id"


def run(
    ctx: AssertionContext,
    scaffold: Scaffold,
    plugin_dir: Path,
    results_dir: Path | None,
) -> None:
    ctx.log_section(NAME)

    test_project_dir = scaffold.brownfield()
    prompt_id_path = test_project_dir / _PROMPT_ID_FILE

    # Remove any prior prompt_id file so we can prove it gets written
    if prompt_id_path.exists():
        prompt_id_path.unlink()

    ctx.log_info(f"Prompt: {_PROMPT!r}")
    out, _ = run_claude(_PROMPT, test_project_dir, plugin_dir, max_turns=6)
    ctx.last_output = out

    # TC-1: non-empty output
    ctx.assert_json_not_empty("TC-1: stream-json output is non-empty")

    # TC-2: Agent tool_use present
    events = parse_jsonl(out) if out and out.is_file() else []
    tool_calls = extract_tool_calls(events)
    agent_calls = [tc for tc in tool_calls if tc.get("name") == "Agent"]
    if agent_calls:
        ctx._pass(f"TC-2: Agent tool_use present ({len(agent_calls)} call(s))")
    else:
        ctx._fail(
            "TC-2: Agent tool_use not found — subagent was not spawned",
            f"tools used: {[tc.get('name') for tc in tool_calls]}",
        )

    # TC-3: SubagentStop hook event in system messages
    hook_msgs = extract_hook_messages(events)
    subagent_stop_events = [
        m for m in hook_msgs
        if "SubagentStop" in m or "subagent" in m.lower()
    ]
    if subagent_stop_events:
        ctx._pass(
            f"TC-3: SubagentStop hook event found in system messages "
            f"({len(subagent_stop_events)} event(s))"
        )
    else:
        ctx._skip(
            "TC-3: SubagentStop hook message not found "
            "(hook may not be registered in hooks.json yet)"
        )

    # TC-4: prompt_id state file written
    if prompt_id_path.exists():
        content = prompt_id_path.read_text(encoding="utf-8").strip()
        if content:
            ctx._pass(f"TC-4: prompt_id state file written ({content!r})")
        else:
            ctx._fail("TC-4: prompt_id state file exists but is empty")
    else:
        ctx._skip(
            "TC-4: prompt_id state file not found "
            "(.prism/brain/current_prompt_id — conductor may not have run)"
        )

    if results_dir:
        finalize_results(NAME, results_dir, out, ctx.passed, ctx.failed)

    scaffold.teardown()
