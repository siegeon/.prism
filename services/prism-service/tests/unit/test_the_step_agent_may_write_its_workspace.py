"""The step agent can write the test file it must write - task 2433fa8a.

task_runner spawns each step agent as `claude -p` with cwd=<task workspace>
(claude_cli.invoke passes cwd=str(work_dir)). The repo's own
`.claude/settings.local.json` cannot reach that agent, because `.claude/` is
gitignored, so `git worktree add` never carries it across. The agent
therefore ran with NO permission config and the harness refused its own
Edit: "Claude requested permissions to edit ... which is a sensitive file".

Observed live on task 1bcb2b24, 2026-09-08. The agent wrote nothing, spent
its whole USD budget, and red_gate correctly refused a step that produced
no test. A drive cannot reach red without writing a test file.

  AC-1  a fresh workspace carries a settings file the agent will read.
  AC-2  the grant is scoped to that worktree, never global.
  AC-3  a workspace made BEFORE this fix self-heals on the next call.
  AC-4  the settings file never dirties the lane's git diff.
"""
from __future__ import annotations

import json
from pathlib import Path

from prism_service.services import task_workspace


SETTINGS = Path(".claude") / "settings.local.json"


def _allow(ws: Path) -> list:
    data = json.loads((ws / SETTINGS).read_text(encoding="utf-8"))
    return data["permissions"]["allow"]


def test_a_workspace_grants_edit_and_write(tmp_path):
    """AC-1: the file exists and names the tools the agent needs. Without
    Edit and Write the write_failing_tests step cannot produce anything."""
    ws = tmp_path / "ws"
    ws.mkdir()
    task_workspace._write_agent_settings(ws)

    assert (ws / SETTINGS).exists(), "the agent reads THIS file, or none"
    allow = _allow(ws)
    for tool in ("Edit", "Write", "MultiEdit"):
        assert any(r.startswith(tool + "(") for r in allow), (tool, allow)


def test_the_grant_is_scoped_to_that_worktree(tmp_path):
    """AC-2 (the likely_misfire guard): the repair must not hand a step
    agent write access to the whole host - only its own workspace."""
    ws = tmp_path / "ws"
    ws.mkdir()
    task_workspace._write_agent_settings(ws)

    for rule in _allow(ws):
        assert rule != "Edit" and rule != "Write" and rule != "MultiEdit", (
            f"bare {rule} grants the whole filesystem")
        assert str(ws) in rule, (rule, str(ws))


def test_an_older_workspace_self_heals(tmp_path):
    """AC-3: task 1bcb2b24's workspace already exists, so a create-path-only
    fix would never reach it. Mirrors _link_web_node_modules' self-heal."""
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    assert not (ws / SETTINGS).exists()

    task_workspace._write_agent_settings(ws)
    assert (ws / SETTINGS).exists()

    # Idempotent: a second call rewrites the same content, never appends.
    before = (ws / SETTINGS).read_text()
    task_workspace._write_agent_settings(ws)
    assert (ws / SETTINGS).read_text() == before


def test_the_settings_file_never_dirties_the_lane_diff():
    """AC-4 (the second likely_misfire guard): the file must land somewhere
    git already ignores, or every lane's diff grows an untracked file."""
    root = Path(__file__).resolve().parents[4]
    ignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert any(ln.strip() in (".claude/", ".claude")
               for ln in ignore.splitlines()), (
        "the settings file lands under .claude/, which must stay ignored")


def test_ensure_workspace_writes_it_on_both_paths():
    """The helper is inert unless ensure_workspace calls it on the create
    path AND the self-heal path - the existing workspace is the one that
    is stuck right now."""
    import inspect
    src = inspect.getsource(task_workspace.ensure_workspace)
    assert src.count("_write_agent_settings") >= 2, src
