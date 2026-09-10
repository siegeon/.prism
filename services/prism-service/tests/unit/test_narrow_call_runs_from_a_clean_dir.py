"""A no-tool node call does not inherit an interactive agent's CLAUDE.md.

MEASURED 2026-09-10 on the live daemon. The declared verify_plan middle is a
1,295-character prompt with no tools, and the call carried 28,560 input tokens.
Decomposed with three `claude -p` runs whose flags matched the live seat
exactly, varying only what the child could see:

    A  trivial 30-char prompt, real task workspace .. input 28,003   125.1s
    B  trivial 30-char prompt, empty scratch dir ...  input  7,602    33.3s
       workspace cost (A - B) ...................... 20,401 tokens

So a THIRTY CHARACTER prompt costs 28,003 input tokens in the task workspace:
about 98% of that node's input is envelope, present regardless of what we ask.
The workspace carries a 75,961-byte CLAUDE.md at its root, which is doctrine
written for an INTERACTIVE CODING AGENT -- reading files, running git, driving
gates. Hand ~20k tokens of "you are an agent that reads files" to the model,
then pass `--tools ""` so it has none, and it role-plays the loop anyway: the
d5808cd1 plan opens '<think>\\n\\n</think>\\n\\nLet me read the file to
understand the context.\\n\\n<file-read>{...}' with INVENTED file contents,
naming prism_service/tasks/reap.py, which does not exist. Task a9f2bec7's
`--tools ""` removed the tools; it could not remove the model's belief that it
had them, because that belief arrives in the memory file.

WHY THE CWD AND NOT `--bare`. `--bare` also skips CLAUDE.md auto-discovery, but
its help says "Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper
(OAuth and keychain are never read)", while `_strip_env` REMOVES
ANTHROPIC_API_KEY under INV-1 and sets ANTHROPIC_AUTH_TOKEN instead. It would
risk the local path and would break every user who is not on an API key.
CLAUDE.md discovery is directory-driven -- `--bare`'s own escape hatch is
"--add-dir (CLAUDE.md dirs)" -- so a clean directory gets the reduction without
touching authentication at all.

THE DIRECTORY IS STABLE ON PURPOSE, not a per-call tempdir. The cwd appears in
the harness system prompt, so a path that changed every call would change the
prompt prefix every call and defeat prefix caching -- and caching is exactly
what makes the envelope cheap when it hits (a draft_story run at 06:15:06 paid
613 fresh tokens against 27,520 cache_read and finished in 23.9s).

WIDE STEPS ARE UNCHANGED. implement_tasks, write_failing_tests and
verify_green_state read and write real files in the task workspace and
legitimately want its CLAUDE.md.
"""

from __future__ import annotations

from pathlib import Path

from prism_service.inference import claude_cli


def test_a_narrow_call_runs_from_a_directory_with_no_claude_md():
    """`allowed_tools=()` is the narrow marker -- the same condition that
    already selects the declared prompt and disables every tool."""
    d = claude_cli._narrow_context_dir()

    assert d.is_dir(), "the clean context directory must exist"
    assert not (d / "CLAUDE.md").exists(), (
        "the whole point: no CLAUDE.md for auto-discovery to find")
    assert not any(d.iterdir()), (
        "anything left in here becomes context for every narrow node call")


def test_the_clean_directory_is_stable_across_calls():
    """A per-call tempdir would change the cwd string in the system prompt on
    every call and defeat prefix caching, which is what makes the envelope
    cheap when it hits."""
    assert claude_cli._narrow_context_dir() == claude_cli._narrow_context_dir()


def test_a_narrow_call_does_not_run_in_the_task_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "task-workspace"
    workspace.mkdir()
    (workspace / "CLAUDE.md").write_text("x" * 75961, encoding="utf-8")

    seen = {}

    def fake_run(cmd, **kw):
        seen["cwd"] = kw.get("cwd")
        seen["cmd"] = cmd
        raise RuntimeError("stop after capturing the invocation")

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    try:
        claude_cli.invoke("draft a plan", work_dir=workspace,
                          plugin_dir=workspace, allowed_tools=())
    except RuntimeError:
        pass

    assert seen["cwd"] == str(claude_cli._narrow_context_dir())
    assert seen["cwd"] != str(workspace)
    # --plugin-dir carries its own discovery, so it must move too.
    assert str(workspace) not in seen["cmd"], (
        "no path into the task workspace may survive on a narrow call")


def test_a_wide_call_still_runs_in_the_task_workspace(tmp_path, monkeypatch):
    """REGRESSION GUARD. The build steps read and write real files there."""
    workspace = tmp_path / "task-workspace"
    workspace.mkdir()

    seen = {}

    def fake_run(cmd, **kw):
        seen["cwd"] = kw.get("cwd")
        raise RuntimeError("stop after capturing the invocation")

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    try:
        claude_cli.invoke("build it", work_dir=workspace, plugin_dir=workspace,
                          allowed_tools=("Read", "Write", "Bash"))
    except RuntimeError:
        pass

    assert seen["cwd"] == str(workspace), (
        "a tool-using step keeps the task workspace and its CLAUDE.md")
