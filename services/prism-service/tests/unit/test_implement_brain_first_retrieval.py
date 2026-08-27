# split from test_implement_brain_first_retrieval.py -- see task 3a3f90da: AC-2..AC-5 need a live drive transcript and cannot gate a single implement_tasks step
"""Pin AC-1, AC-6 and AC-7 of task 3a3f90da "Make the Brain the default
retrieval path on drives" (see task.plan_doc for the full design this file
follows).

These are the fixture-driven / source-reading ACs that need no live drive:
AC-1 pins the DISK-RETRIEVAL counter's own definition (FR-1), AC-7 pins the
refuse-on-absent-evidence contract (NFR-3), and AC-6 pins that implement.js
names Bash-shelled retrieval by name (necessary but explicitly NOT
sufficient -- AC-3/AC-5, in the sibling live-drive file, prove the
behaviour actually moved).

AC-2, AC-3, AC-4 and AC-5 -- which require a REAL fresh `implement` drive's
subagent transcripts on disk -- live in
test_implement_brain_first_retrieval_live.py so they cannot block this
task's green_gate on a live drive that is itself a separate, later action.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_IMPLEMENT_JS = _REPO_ROOT / ".claude" / "workflows" / "implement.js"

LOCATE_MARKER = "Build a brain-first context_summary"

# FR-1: DISK-RETRIEVAL = the Grep tool, OR a Bash call whose command text
# shells out to a disk-search program. Word-boundary match so e.g. "cathedral"
# or "sedan" in an unrelated command does not false-positive.
_DISK_CMD_RE = re.compile(r"(?<![\w/-])(grep|rg|ag|sed|awk|find|cat|head|tail)(?![\w/-])")


def _is_disk_retrieval(tool_name: str, tool_input: dict) -> bool:
    if tool_name == "Grep":
        return True
    if tool_name == "Bash":
        cmd = str((tool_input or {}).get("command", ""))
        return bool(_DISK_CMD_RE.search(cmd))
    return False


def _walk_events(path: Path):
    """Yield dicts, in file order, for every tool_use / tool_result / text
    block across a transcript JSONL's events. Shared walk every AC below is
    built from -- modelled on the event shape
    prism_service.services.claude_transcripts.parse_session_metrics already
    parses (message.content list of {"type": tool_use|tool_result|text})."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = evt.get("message") or {}
        role = msg.get("role") or evt.get("type") or ""
        content = msg.get("content")
        if isinstance(content, str):
            if content:
                yield {"kind": "text", "role": role, "text": content}
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                yield {
                    "kind": "tool_use",
                    "role": role,
                    "name": block.get("name") or "",
                    "input": block.get("input") or {},
                    "tool_use_id": block.get("id") or block.get("tool_use_id") or "",
                }
            elif btype == "tool_result":
                raw = block.get("content")
                if isinstance(raw, list):
                    raw = " ".join(
                        x.get("text", "") for x in raw
                        if isinstance(x, dict) and x.get("text")
                    )
                yield {
                    "kind": "tool_result",
                    "role": role,
                    "tool_use_id": block.get("tool_use_id") or "",
                    "text": str(raw or ""),
                }
            elif btype == "text":
                t = block.get("text") or ""
                if t:
                    yield {"kind": "text", "role": role, "text": t}


def _scan(path: Path):
    """(tool_name, tool_input) pairs in order -- the walk AC-1 needs."""
    for ev in _walk_events(path):
        if ev["kind"] == "tool_use":
            yield ev["name"], ev["input"]


def _transcript_root() -> Path:
    override = os.environ.get("PRISM_BRAIN_FIRST_TRANSCRIPT_ROOT", "").strip()
    return Path(override) if override else Path.home() / ".claude" / "projects"


def _locate_drive_transcripts() -> list[Path]:
    """Find the subagent (+ parent) transcripts for the fresh `implement`
    drive under measurement. Raises AssertionError naming exactly what is
    missing -- NFR-3 forbids returning an empty-but-passing result."""
    session = os.environ.get("PRISM_BRAIN_FIRST_DRIVE_SESSION", "").strip()
    root = _transcript_root()
    if not session:
        raise AssertionError(
            "PRISM_BRAIN_FIRST_DRIVE_SESSION is not set. AC-2..AC-5 measure a "
            "REAL fresh `implement` drive's subagent transcripts -- run "
            "Workflow({name:\"implement\", ...}) and pass its session/run id "
            "via this env var. Refusing to report a vacuous pass "
            "(task 3a3f90da NFR-3)."
        )
    if not root.is_dir():
        raise AssertionError(
            f"Transcript root {root} does not exist "
            "(set PRISM_BRAIN_FIRST_TRANSCRIPT_ROOT if ~/.claude/projects is wrong)."
        )
    hits: list[Path] = []
    for proj_dir in sorted(root.iterdir()):
        if not proj_dir.is_dir():
            continue
        session_dir = proj_dir / session
        subagents = session_dir / "subagents"
        run = os.environ.get("PRISM_BRAIN_FIRST_DRIVE_RUN", "").strip()
        if run:
            subagents = subagents / "workflows" / run
        if subagents.is_dir():
            hits.extend(sorted(subagents.rglob("*.jsonl")))
        # When PRISM_BRAIN_FIRST_DRIVE_RUN scopes the measurement to ONE wf_
        # run, the parent session transcript is NOT part of that drive: it
        # carries the main session's own retrieval traffic across every drive
        # it hosted (measured 2026-08-26: ~460 disk calls of a 507 total came
        # from the parent file alone), so including it drowns the per-run
        # ratio AC-5 exists to measure.
        parent_file = proj_dir / f"{session}.jsonl"
        if parent_file.is_file() and not run:
            hits.append(parent_file)
    if not hits:
        raise AssertionError(
            f"No transcripts found for session {session!r} under {root} "
            "(looked for <project>/<session>/subagents/**/*.jsonl and "
            "<project>/<session>.jsonl). AC-2..AC-5 cannot be measured "
            "without a fresh drive's real transcripts on disk "
            "(task 3a3f90da NFR-3)."
        )
    return hits


# ---------------------------------------------------------------------------
# AC-1 -- fixture-driven, no live drive needed.
# ---------------------------------------------------------------------------

def test_ac1_disk_retrieval_counts_bash_shelled_grep_not_just_the_grep_tool(tmp_path):
    xscript = tmp_path / "agent-abc.jsonl"
    xscript.write_text(
        json.dumps({
            "sessionId": "s1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "grep -rn foo src/"}},
                ],
            },
        }) + "\n",
        encoding="utf-8",
    )
    calls = list(_scan(xscript))
    assert len(calls) == 1
    name, inp = calls[0]
    assert _is_disk_retrieval(name, inp) is True, (
        "A Bash call shelling out to grep must count as disk retrieval."
    )
    grep_tool_calls = sum(1 for n, _ in calls if n == "Grep")
    assert grep_tool_calls == 0, (
        "This fixture never calls the Grep TOOL -- a Grep-tool-only counter "
        "would report 0 disk-retrieval calls here, which is the exact "
        "misfire this AC exists to prevent."
    )


# ---------------------------------------------------------------------------
# AC-7 -- fixture-driven, no live drive needed.
# ---------------------------------------------------------------------------

def test_ac7_refuses_to_pass_on_absent_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_BRAIN_FIRST_TRANSCRIPT_ROOT", str(tmp_path))
    monkeypatch.setenv("PRISM_BRAIN_FIRST_DRIVE_SESSION", "no-such-session")
    (tmp_path / "some-project").mkdir()
    try:
        _locate_drive_transcripts()
    except AssertionError as exc:
        assert "no-such-session" in str(exc) or "No transcripts found" in str(exc)
        return
    raise AssertionError(
        "_locate_drive_transcripts() must raise loudly on an empty transcript "
        "root instead of returning an empty list silently."
    )


# ---------------------------------------------------------------------------
# AC-6 -- source-reading, necessary but explicitly NOT sufficient (AC-3/AC-5
# in the sibling live-drive file prove the behaviour actually moved).
# ---------------------------------------------------------------------------

def test_ac6_implement_js_names_bash_shelled_retrieval_by_name():
    src = _IMPLEMENT_JS.read_text(encoding="utf-8")

    knowledge_idx = src.find("Brain is the primary repository")
    assert knowledge_idx != -1, "KNOWLEDGE block marker not found in implement.js"
    knowledge_window = src[knowledge_idx: knowledge_idx + 800]
    assert re.search(r"\bBash\b", knowledge_window), (
        "KNOWLEDGE block does not name Bash-shelled retrieval near its "
        "Grep/Glob/Read fallback line -- disk traffic actually goes through "
        "Bash (grep/sed/find/...), not the Grep tool (memory mx-53efa3)."
    )
    for tool in ("grep", "rg", "sed", "find"):
        assert tool in knowledge_window, (
            f"KNOWLEDGE block does not name {tool!r} as Bash-shelled retrieval."
        )

    locate_idx = src.find(LOCATE_MARKER)
    assert locate_idx != -1, "Locate step marker not found in implement.js"
    locate_window = src[max(0, locate_idx - 400): locate_idx + 400]
    assert re.search(r"\bBash\b", locate_window), (
        "Locate step prompt does not name Bash-shelled retrieval near "
        "'disk grep only for gaps'."
    )
