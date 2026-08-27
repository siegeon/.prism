"""Pin task 0e2c82f3 "Brain retrieval costs a tool load, Bash does not".

AC-1 classifies transcript events (disk / brain / toolsearch) on a fixture
JSONL. AC-2 / AC-3 pin the new `drive` MCP tool profile and that every
existing profile alias is unchanged. AC-4 / AC-5 parse a REAL fresh drive's
subagent transcripts (locator + counter imported from the 3a3f90da suites,
FR-1 / FR-2: one counter, never two). AC-4 / AC-5 SKIP with the path they
looked for when no drive transcript exists. They never PASS on no data.

Env for AC-4 / AC-5:
  PRISM_BRAIN_FIRST_DRIVE_PROFILE   -- must be "drive"; else skip
  PRISM_BRAIN_FIRST_DRIVE_SESSION   -- the drive's session id
  PRISM_BRAIN_FIRST_TRANSCRIPT_ROOT -- defaults to ~/.claude/projects
  PRISM_BRAIN_FIRST_DRIVE_RUN       -- optional wf_<id> run dir
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_bf = _load("test_implement_brain_first_retrieval")
_live = _load("test_implement_brain_first_retrieval_live")

LOCATE_MARKER = _bf.LOCATE_MARKER
_is_disk_retrieval = _bf._is_disk_retrieval
_walk_events = _bf._walk_events

BRAIN_VERBS = {
    "brain_search", "brain_understand", "brain_call_chain",
    "brain_find_references", "context_bundle",
}
DRIVE_REQUIRED = BRAIN_VERBS | {
    "conductor_work", "task_list", "task_update", "task_link_session",
    "memory_recall", "memory_store",
}
# Snapshot of TOOL_PROFILE_ALIASES at d710f4df (mcp/tools.py:2017-2027).
ALIASES_BEFORE = {
    "all": "all", "default": "interactive", "core": "interactive",
    "interactive": "interactive", "admin": "admin", "project": "admin",
    "hooks": "hooks", "telemetry": "hooks", "learning": "learning",
    "automation": "automation", "hooks_api": "automation",
}


def _classify(tool_name: str, tool_input: dict) -> str | None:
    """disk | brain | toolsearch | None -- the ONE counter (FR-1)."""
    if tool_name == "ToolSearch":
        return "toolsearch"
    if tool_name in BRAIN_VERBS or tool_name.endswith(
        tuple("__" + v for v in BRAIN_VERBS)
    ):
        return "brain"
    if _is_disk_retrieval(tool_name, tool_input):
        return "disk"
    return None


def _event(name: str, inp: dict) -> str:
    return json.dumps({"type": "assistant", "message": {"role": "assistant",
        "content": [{"type": "tool_use", "name": name, "input": inp}]}})


def _write_fixture(tmp_path: Path) -> Path:
    rows = [
        _event("Grep", {"pattern": "x"}),
        *[_event("Bash", {"command": f"{p} foo bar"})
          for p in ("grep", "rg", "ag", "sed", "awk", "find", "cat", "head", "tail")],
        *[_event(v, {"query": "q"}) for v in sorted(BRAIN_VERBS)],
        _event("mcp__prism__brain_search", {"query": "q"}),
        _event("ToolSearch", {"query": "select:brain_search"}),
        _event("Bash", {"command": "echo cathedral"}),
        _event("Bash", {"command": "ls sedan"}),
        _event("Read", {"file_path": "/x"}),
    ]
    p = tmp_path / "fixture.jsonl"
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return p


def _counts(paths: list[Path]) -> dict[str, int]:
    out = {"disk": 0, "brain": 0, "toolsearch": 0}
    for p in paths:
        for ev in _walk_events(p):
            if ev["kind"] != "tool_use":
                continue
            k = _classify(ev["name"], ev["input"])
            if k:
                out[k] += 1
    return out


# ---- AC-1 -----------------------------------------------------------------

def test_classify_fixture_events(tmp_path):
    """AC-1: the counter tags every kind on one fixture and does NOT tag
    `cathedral` / `sedan` / Read as disk retrieval."""
    got = _counts([_write_fixture(tmp_path)])
    assert got == {"disk": 10, "brain": 6, "toolsearch": 1}


def test_classify_negatives_are_not_disk():
    assert _classify("Bash", {"command": "echo cathedral"}) is None
    assert _classify("Bash", {"command": "ls sedan"}) is None
    assert _classify("Read", {"file_path": "/x"}) is None
    assert _classify("Bash", {"command": "grep -n foo bar.py"}) == "disk"


# ---- AC-2 -----------------------------------------------------------------

def test_drive_profile_members():
    """AC-2: `drive` keeps the retrieval + conductor verbs and nothing from the
    admin-only / hooks / learning / automation surfaces."""
    from prism_service.mcp import tools as t

    assert "drive" in t.TOOL_PROFILE_ALIASES, "no `drive` alias in TOOL_PROFILE_ALIASES"
    names = t.tool_names_for_profile("drive")
    assert names != {tool.name for tool in t.TOOLS}, "drive resolved to the `all` set"
    assert DRIVE_REQUIRED <= names, sorted(DRIVE_REQUIRED - names)
    banned = ((t.ADMIN_TOOL_NAMES - t.INTERACTIVE_TOOL_NAMES) | t.HOOK_TOOL_NAMES
              | t.LEARNING_TOOL_NAMES | t.AUTOMATION_TOOL_NAMES) - DRIVE_REQUIRED
    assert not (names & banned), sorted(names & banned)
    assert names == set(getattr(t, "DRIVE_TOOL_NAMES", set())) & {x.name for x in t.TOOLS}


# ---- AC-3 -----------------------------------------------------------------

def test_existing_profiles_unchanged():
    """AC-3: every alias that existed before this task resolves the same,
    and the default profile is still `interactive`."""
    from prism_service.mcp import tools as t

    for alias, target in ALIASES_BEFORE.items():
        assert t.TOOL_PROFILE_ALIASES.get(alias) == target, alias
    all_names = {tool.name for tool in t.TOOLS}
    assert t.tool_names_for_profile(None) == t.INTERACTIVE_TOOL_NAMES & all_names
    assert t.tool_names_for_profile("all") == all_names
    assert t.tool_names_for_profile("automation") == t.AUTOMATION_TOOL_NAMES & all_names


# ---- AC-4 / AC-5 (live drive transcripts) ---------------------------------

def _drive_transcripts() -> list[Path]:
    profile = os.environ.get("PRISM_BRAIN_FIRST_DRIVE_PROFILE", "").strip().lower()
    if profile != "drive":
        pytest.skip("PRISM_BRAIN_FIRST_DRIVE_PROFILE is not 'drive'; a transcript "
                    "from a non-drive-profile run proves nothing here (AC-4)")
    session = os.environ.get("PRISM_BRAIN_FIRST_DRIVE_SESSION", "").strip()
    root = _live._transcript_root()
    run = os.environ.get("PRISM_BRAIN_FIRST_DRIVE_RUN", "").strip()
    looked = f"{root}/<project>/{session or '<session>'}/subagents" + (
        f"/workflows/{run}" if run else "")
    try:
        return _live._locate_drive_transcripts()
    except AssertionError as exc:
        pytest.skip(f"no drive transcript under {looked}: {exc}")


def _locate_files(paths: list[Path]) -> list[Path]:
    return [p for p in paths if LOCATE_MARKER in p.read_text("utf-8", errors="replace")]


def test_locate_no_toolsearch_before_brain():
    """AC-4: in the Locate transcript, brain_search precedes the first disk
    retrieval and NO ToolSearch call sits between step start and it."""
    locate = _locate_files(_drive_transcripts())
    assert locate, "no subagent transcript carries the Locate marker"
    for path in locate:
        seq = [(i, _classify(ev["name"], ev["input"]))
               for i, ev in enumerate(e for e in _walk_events(path) if e["kind"] == "tool_use")]
        first = {k: next((i for i, c in seq if c == k), None)
                 for k in ("brain", "disk", "toolsearch")}
        assert first["brain"] is not None, f"{path}: no Brain retrieval call at all"
        assert first["disk"] is None or first["brain"] < first["disk"], (
            f"{path}: disk retrieval at event {first['disk']} precedes brain at {first['brain']}")
        assert first["toolsearch"] is None or first["toolsearch"] > first["brain"], (
            f"{path}: ToolSearch at event {first['toolsearch']} precedes brain_search "
            f"at {first['brain']} -- stop_if #1 (harness tool load), see AC-6")


def test_drive_wide_ratio():
    """AC-5: disk-to-Brain ratio across the whole drive is at most 5:1."""
    got = _counts(_drive_transcripts())
    ratio = got["disk"] / got["brain"] if got["brain"] else float("inf")
    print(f"disk={got['disk']} brain={got['brain']} ratio={ratio:.2f}")
    assert got["brain"] > 0, f"no Brain retrieval in the drive: {got}"
    assert ratio <= 5, f"disk={got['disk']} brain={got['brain']} ratio={ratio:.2f} > 5"
