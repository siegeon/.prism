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


# ---- task 9b0f7c4b: wire implement step agents to the drive profile ------
# Source-reading tests (the PRISM workflow scripts have NO JS test runner, so
# a prompt contract is pinned by parsing the ACTUAL implement.js source). Each
# assertion parses the enclosing `agent(` block, never a fixed character window.

_REPO_ROOT = _HERE.parents[3]
_IMPLEMENT_JS = _REPO_ROOT / ".claude" / "workflows" / "implement.js"
_MCP_JSON = _REPO_ROOT / ".mcp.json"
# Fixed bare-verb list from AC-3 (task 9b0f7c4b). `conductor_advance`,
# `conductor_gate`, `workflow_state` are named only as "do not use" prose.
PROMPT_BARE_VERBS = {
    "prism_status", "conductor_work", "task_list", "task_update",
    "task_link_session", "brain_search", "brain_understand", "brain_call_chain",
    "brain_find_references", "context_bundle", "memory_recall", "memory_store",
    "janitor_check", "janitor_submit",
}


def _implement_src() -> str:
    assert _IMPLEMENT_JS.is_file(), f"missing {_IMPLEMENT_JS}"
    return _IMPLEMENT_JS.read_text("utf-8")


def _agent_block(src: str, label: str) -> str:
    """Return the full `agent(...)` call whose options carry `label: '<label>'`.
    Walks parens while skipping quoted / template strings, so a `(` inside a
    prompt never ends the block early."""
    tag = f"label: '{label}'"
    at = src.find(tag)
    assert at >= 0, f"no agent() carries {tag} in implement.js"
    start = src.rfind("agent(", 0, at)
    assert start >= 0, f"no `agent(` precedes {tag}"
    i, depth, quote = start + len("agent("), 1, None
    while i < len(src) and depth:
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    assert depth == 0, f"unbalanced agent( block for {tag}"
    return src[start:i]


def test_mcp_json_connects_drive_profile():
    """AC-1: the PRISM MCP entry connects with tool_profile=drive, same
    host/port/project (server.py reads the profile from the URL query only)."""
    url = json.loads(_MCP_JSON.read_text("utf-8"))["mcpServers"]["prism"]["url"]
    assert url.startswith("http://127.0.0.1:7777/mcp/"), url
    assert "project=prism" in url, url
    assert "tool_profile=drive" in url, f"step agents connect without the drive profile: {url}"


def test_implement_prompts_do_not_preload_prism_via_toolsearch():
    """AC-2: no step prompt instructs a ToolSearch preload of PRISM verbs
    before the first PRISM call (the harness deferral is stop_if #1)."""
    hits = [f"{n}: {line.strip()[:80]}" for n, line in
            enumerate(_implement_src().splitlines(), 1)
            if 'ToolSearch("select:mcp__prism__' in line]
    assert not hits, "ToolSearch preload of PRISM verbs still in implement.js:\n" + "\n".join(hits)


def test_implement_prompt_verbs_are_drive_profile_members():
    """AC-3: every PRISM MCP verb named in implement.js is served by the
    drive profile (DRIVE_TOOL_NAMES); a non-member is invisible to a step agent."""
    import re
    from prism_service.mcp import tools as t

    src = _implement_src()
    named = set(re.findall(r"mcp__prism__(\w+)", src))
    named |= {v for v in PROMPT_BARE_VERBS if re.search(rf"\b{v}\b", src)}
    assert named, "no PRISM verb named in implement.js at all"
    missing = named - set(t.DRIVE_TOOL_NAMES)
    assert not missing, f"verbs named in implement.js but absent from DRIVE_TOOL_NAMES: {sorted(missing)}"


def test_preflight_reports_tool_profile():
    """AC-4: pre-flight schema carries `tool_profile`, and the pre-flight
    agent() block halts with `expected drive` when the profile is not drive."""
    src = _implement_src()
    s = src.find("const PREFLIGHT_SCHEMA = {")
    assert s >= 0, "no PREFLIGHT_SCHEMA in implement.js"
    schema = src[s:src.find("\n}\n", s)]
    assert "tool_profile" in schema, "PREFLIGHT_SCHEMA has no tool_profile field"
    block = _agent_block(src, "pre-flight")
    assert "tool_profile" in block, "pre-flight agent() block never reads tool_profile"
    assert "expected drive" in block, "pre-flight agent() block has no `expected drive` halt_reason"
