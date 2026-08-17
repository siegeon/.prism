"""Halve the per-edit hook cost (task 86fac34e) — machine-checkable half.

MEASURED (2026-08-01 doctor pass): a single Edit paid TWO cold python
starts because two PostToolUse entries' matchers both covered Edit/Write
(`prism-feedback-signal.py` and `prism-edit-learn.py`), and Stop paid the
same doubling (`prism-stop.py` + `prism-idle-rebuild.py`) — ~4,400 cold
interpreter starts and ~2.6h of blocking wall-time in a 3-week window.
The fix merges each pair into one dispatcher process. This guard parses
`.claude/settings.json` and fails if any TWO hook entries would ever fire
for the same tool event again.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent.parent.parent.parent
_SETTINGS = _REPO_ROOT / ".claude" / "settings.json"

# .claude/settings.json is the OWNER's local hook wiring (untracked), so a
# bare CI checkout has none — the guard enforces wherever hooks are wired.
pytestmark = pytest.mark.skipif(
    not _SETTINGS.exists(),
    reason=".claude/settings.json not present (bare checkout — no hooks wired)")

_TOOLS = ["Edit", "Write", "Read", "NotebookEdit", "Bash", "Skill",
          "mcp__prism__brain_search"]


def _hooks() -> dict:
    return json.loads(_SETTINGS.read_text(encoding="utf-8")).get("hooks", {})


def _matches(matcher: str, tool: str) -> bool:
    if not matcher:
        return True  # empty matcher = every tool
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return matcher == tool


def test_no_two_posttooluse_entries_cover_the_same_tool():
    entries = _hooks().get("PostToolUse", [])
    for tool in _TOOLS:
        firing = [e.get("matcher", "") for e in entries
                  if _matches(e.get("matcher", ""), tool)]
        assert len(firing) <= 1, (
            f"{len(firing)} PostToolUse entries fire for one {tool} call "
            f"({firing}) — each is a cold python start; a single Edit "
            f"used to pay ~4.5s this way (task 86fac34e)")


def test_stop_registers_exactly_one_command():
    entries = _hooks().get("Stop", [])
    commands = [h for e in entries for h in (e.get("hooks") or [])]
    assert len(commands) <= 1, (
        f"Stop registers {len(commands)} hook commands — the metrics and "
        f"graph-rebuild halves belong in ONE dispatcher process "
        f"(task 86fac34e); got {[c.get('command') for c in commands]}")


def test_the_dispatchers_still_carry_both_side_effects():
    """Merging must not DROP a side effect (the task's own stop_if): the
    PostToolUse dispatcher must still dispatch edit-learn, and the Stop
    dispatcher must still flush the graph-dirty sentinel."""
    fs = (_REPO_ROOT / ".prism" / "hooks" / "prism-feedback-signal.py"
          ).read_text(encoding="utf-8")
    assert "prism-edit-learn.py" in fs and ".handle(" in fs or "mod.handle(" in fs, (
        "prism-feedback-signal.py no longer dispatches edit-learn — the "
        "in-session Brain ingest silently stopped")
    st = (_REPO_ROOT / ".prism" / "hooks" / "prism-stop.py"
          ).read_text(encoding="utf-8")
    assert "prism-idle-rebuild.py" in st, (
        "prism-stop.py no longer dispatches the graph-rebuild flush")
