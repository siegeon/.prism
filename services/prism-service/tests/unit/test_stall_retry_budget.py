"""The draft story node finishes inside its own budget (task 1c42ee74).

MEASURED FAILURE, task d5808cd1 on the live daemon 2026-09-09/10. Seven
`claude -p` runs at `task-runner@draft_story#d5808cd1` and
`resume-actuator@draft_story#d5808cd1` in
`~/.prism/claude_runs/manifest.jsonl`, each `exit -9 dur 900.1`. The task
never left draft_story, the runner stalled it three times, and
prism-resume-actuator spent its whole ceiling of 12 dispatches on a step
that could not report.

THREE FACTS THE DEFECT RESTS ON:

1. `allowed_tools=()` DOES NOT DISABLE TOOLS. `_build_cmd` skips
   `--allowedTools` when the tuple is empty, so the child gets claude's
   default toolset AND every configured MCP server. The `system/init` event
   of run a141a41ee2ae proves it: 40+ built-in tools and 13 MCP servers,
   for a call whose declared plan is a single no-tool text generation. The
   model then spent the whole 900 s in `mcp__prism__brain_*` round trips
   and never emitted a `result` event. PRISM has known this since v5.3.14
   ("`allowed_tools=()` doesn't disable claude's default tool set") and
   worked around it in the PROMPT each time instead of on the command line;
   every later call site inherited the wrong belief.

2. `exit=-9` IS PRISM'S OWN TIMEOUT, not a host SIGKILL.
   `claude_cli.TIMEOUT_EXIT_CODE == -9`, returned when `subprocess.run`
   raises `TimeoutExpired` at `timeout_s`. Reported as
   "exit=-9, no usable output" it is indistinguishable from a crash, which
   is what sent this diagnosis hunting for an OOM kill that never happened.

3. THE RETRY LADDER PAID FULL PRICE EVERY TIME. Three attempts at the
   900 s bound is 45 minutes of wall clock on a step whose declared bound
   is 120 s.

These tests assert the LITERAL expected outcome each clause names, never a
function compared against its own output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_service.inference import claude_cli
from prism_service.services import task_runner

# Same hermetic resolution as test_task_runner_honors_declared_node_plan:
# _behavior_dir's fallback is a dev machine's home, so read the catalog
# committed next to this test file instead.
_REPO_BEHAVIORS = (Path(__file__).resolve().parents[4]
                   / ".prism" / "behaviors" / "conductor")


@pytest.fixture(autouse=True)
def _hermetic_behavior_dir(monkeypatch):
    monkeypatch.setattr(
        task_runner, "_behavior_dir", lambda project: _REPO_BEHAVIORS)


# ----------------------------------------------------------------------
# AC-1 -- a no-tool step really runs with no tools
# ----------------------------------------------------------------------

def test_empty_allowed_tools_disables_every_tool_on_the_command_line():
    """`allowed_tools=()` must produce a command that grants NO tools.

    `--tools ""` disables the built-in set; `--strict-mcp-config` with no
    `--mcp-config` leaves no MCP server configured. Verified live against
    claude 2.1.267: the resulting `system/init` event reports
    `"tools": []` and `"mcp_servers": []`.
    """
    cmd = claude_cli._build_cmd(
        "draft a story", "/tmp/ws", "haiku", 0.5, 4, allowed_tools=())

    assert "--allowedTools" not in cmd, (
        "an empty tuple must not fall through to claude's default toolset")
    assert "--tools" in cmd, "no-tools mode must name --tools explicitly"
    assert cmd[cmd.index("--tools") + 1] == "", (
        'claude disables every built-in tool on --tools ""')
    assert "--strict-mcp-config" in cmd, (
        "an MCP server left configured re-grants tools through the back door")


def test_a_named_tool_scope_is_unchanged_by_the_no_tools_branch():
    """The explicit-scope path keeps its exact previous command shape."""
    cmd = claude_cli._build_cmd(
        "read something", "/tmp/ws", "", 0.0, 3,
        allowed_tools=("Read", "Grep"))

    assert "--allowedTools" in cmd
    idx = cmd.index("--allowedTools")
    assert cmd[idx + 1:idx + 3] == ["Read", "Grep"]
    assert "--tools" not in cmd
    assert "--strict-mcp-config" not in cmd


# ----------------------------------------------------------------------
# AC-2 -- a budget kill says which budget it exceeded
# ----------------------------------------------------------------------

class _KilledResult:
    """A claude_cli result shaped like the timeout return path."""

    exit_code = claude_cli.TIMEOUT_EXIT_CODE
    duration_s = 900.1
    usage = None
    run_id = ""
    structured_output = None

    def final_text(self) -> str:
        return ""

    def graceful_budget_stop(self) -> bool:
        return False


def test_a_budget_kill_names_the_budget_and_the_elapsed_time():
    """The reason must let a reader tell a timeout from a crash.

    The live rows said `exit=-9, no usable output` seven times. That string
    names neither the limit nor how long the step actually ran.
    """
    reason = task_runner._failure_reason(_KilledResult(), budget_s=900.0)

    assert "900s budget" in reason, "the limit that stopped the step"
    assert "900.1s" in reason, "how long it actually ran"
    assert reason != "exit=-9, no usable output"
    assert "no usable output" not in reason


def test_a_crash_is_still_reported_as_a_crash():
    """A non-timeout failure keeps its own wording -- a timeout message on
    a crash is the same dishonesty in the other direction."""

    class _Crashed(_KilledResult):
        exit_code = 1
        duration_s = 3.5

    reason = task_runner._failure_reason(_Crashed(), budget_s=900.0)

    assert "budget" not in reason
    assert "exit=1" in reason


# ----------------------------------------------------------------------
# AC-3 -- the ladder stops paying full price after the first kill
# ----------------------------------------------------------------------

def test_the_draft_story_plan_carries_its_declared_wall_clock():
    """draft-story-loop.json declares `timeoutSeconds: 120` on its agentic
    middle. The literal 120, not the reader applied to itself."""
    plan = task_runner._node_plan("prism", "draft_story")

    assert plan is not None
    assert plan["timeout_s"] == 120.0


def test_the_attempt_after_a_kill_runs_on_the_declared_bound():
    """AC-3: after a budget kill the seat must not spend a further full
    budget on the identical step.

    First attempt: the runner's own 900 s. Second: the node's declared
    120 s, so a wedged draft_story costs 900 + 120 + 120 = 1140 s across
    the three-attempt ladder instead of 2700 s.
    """
    plan = task_runner._node_plan("prism", "draft_story")

    first = task_runner._invoke_budget("draft_story", plan, narrow=True)
    after = task_runner._invoke_budget(
        "draft_story", plan, narrow=True, after_kill=True)

    assert first["timeout_s"] == 900.0
    assert after["timeout_s"] == 120.0
    assert after["timeout_s"] < first["timeout_s"]


def test_a_kill_does_not_shrink_a_step_with_no_declared_bound():
    """implement_tasks declares no plan, so there is no smaller bound to
    fall back to and the runner's own budget stands. A kill must never
    invent a tighter clock out of nothing."""
    before = task_runner._invoke_budget("implement_tasks", None, narrow=False)
    after = task_runner._invoke_budget(
        "implement_tasks", None, narrow=False, after_kill=True)

    assert after["timeout_s"] == before["timeout_s"]
