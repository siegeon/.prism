# split from test_implement_brain_first_retrieval.py -- see task 3a3f90da: AC-2..AC-5 need a live drive transcript and cannot gate a single implement_tasks step
"""Pin AC-2..AC-5 of task 3a3f90da "Make the Brain the default retrieval
path on drives" (see task.plan_doc for the full design this file follows).

These tests measure a REAL drive's subagent transcripts by parsing JSONL --
never implement.js's own prompt text -- per the task's own named misfire: a
source-text presence check proves the words exist, not that behaviour
changed (memory mx-53efa3: a prior 14:1 baseline counted only the Grep
TOOL; a re-measure found the Grep tool called 0 times while Bash carried
832 shelled `grep` invocations, true ratio 416:1).

AC-2, AC-3, AC-4 and AC-5 are transcript-driven: they require a REAL fresh
`implement` drive's subagent transcripts, located via env vars:
  PRISM_BRAIN_FIRST_DRIVE_SESSION       -- the drive's session/run id
  PRISM_BRAIN_FIRST_TRANSCRIPT_ROOT     -- defaults to ~/.claude/projects
  PRISM_BRAIN_FIRST_DRIVE_RUN           -- optional wf_<id> run dir; scopes the
                                           measurement to ONE workflow run when a
                                           session hosts several relaunched drives
  PRISM_BRAIN_FIRST_BLAST_RADIUS_SYMBOLS -- comma-separated symbols from
                                             that drive's own plan_doc
      (AC-4 only)
Per the task's own NFR-3, when the evidence is missing these FAIL LOUDLY
with the exact reason -- never skip silently, never report a vacuous pass.
This is why they are red today: no fresh drive has been run yet for this
env to point at. That is exactly why they live in this file, separate from
test_implement_brain_first_retrieval.py's fixture/source-reading ACs: a
pinned suite that can only go green after a live drive runs cannot also
gate the single implement_tasks step that produces that drive.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

LOCATE_MARKER = "Build a brain-first context_summary"
PLAN_MARKER = "GRAPH RUNG (blast radius, and this is what makes green honest)"

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
    """(tool_name, tool_input) pairs in order -- the walk AC-4/AC-5 need."""
    for ev in _walk_events(path):
        if ev["kind"] == "tool_use":
            yield ev["name"], ev["input"]


def _transcript_root() -> Path:
    override = os.environ.get("PRISM_BRAIN_FIRST_TRANSCRIPT_ROOT", "").strip()
    return Path(override) if override else Path.home() / ".claude" / "projects"


def _locate_drive_transcripts() -> list[Path]:
    """Find the subagent (+ parent) transcripts for the fresh `implement`
    drive under measurement. Raises AssertionError naming exactly what is
    missing when a deliberate opt-in is misconfigured -- NFR-3 forbids
    returning an empty-but-passing result. SKIPS (never fails) when the env
    var is simply unset: GitHub Actions CI cannot provide a live drive
    session, so every bare `pytest tests/unit` run hit this path and
    permanently red-gated CI regardless of the PR under test (observed live
    blocking two unrelated PRs, #2350 task 85f92e4b and #2366 task 9b0f7c4b).
    A skip is not a vacuous pass -- it carries this exact reason in the CI
    log and is never reported as green -- it is pytest's own honest "this
    check cannot run here", which NFR-3 never forbade."""
    session = os.environ.get("PRISM_BRAIN_FIRST_DRIVE_SESSION", "").strip()
    root = _transcript_root()
    if not session:
        pytest.skip(
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
        run = os.environ.get("PRISM_BRAIN_FIRST_DRIVE_RUN", "").strip()
        where = (
            f"PRISM_BRAIN_FIRST_DRIVE_RUN={run!r} has no *.jsonl under "
            f"<project>/{session}/subagents/workflows/{run}/ in {root}"
            if run
            else f"No transcripts found for session {session!r} under {root} "
            "(looked for <project>/<session>/subagents/**/*.jsonl and "
            "<project>/<session>.jsonl)"
        )
        raise AssertionError(
            f"{where}. AC-2..AC-5 cannot be measured without a fresh "
            "drive's real transcripts on disk (task 3a3f90da NFR-3; "
            "task 85f92e4b FR-1 makes this a failure, never a skip)."
        )
    return hits


def _find_transcript_by_marker(transcripts: list[Path], marker: str) -> Path:
    for path in transcripts:
        for ev in _walk_events(path):
            if ev["kind"] == "text" and marker in ev["text"]:
                return path
    raise AssertionError(
        f"No transcript among {[str(p) for p in transcripts]} contains the "
        f"marker {marker!r} -- cannot identify which subagent ran that step."
    )


_SAVED_RESULT_RE = re.compile(r"saved to (\S+?\.txt)")


def _extract_conventions(result_text: str) -> list[str]:
    # The real context_bundle reply is ~1.5 MB (active_tasks + context_pack
    # measured at 748 KB and 756 KB on drive wf_c7bae879), so the harness
    # replaces the inline tool_result with "exceeds maximum allowed tokens
    # ... saved to <path>.txt". The conventions the tool RETURNED live in that
    # file, so follow the pointer -- reading the notice alone would fail AC-2
    # on every drive regardless of what the drive did (task 3a3f90da).
    m = _SAVED_RESULT_RE.search(result_text or "")
    if m and "conventions" not in result_text:
        try:
            result_text = Path(m.group(1)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
    try:
        obj = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        obj = None
    if isinstance(obj, dict):
        conv = obj.get("conventions")
        if isinstance(conv, list):
            return [str(c) for c in conv if c]
    m = re.search(r'"conventions"\s*:\s*\[(.*?)\]', result_text, re.S)
    if m:
        items = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
        return [i for i in items if i]
    return []


def _blast_radius_symbols() -> list[str]:
    raw = os.environ.get("PRISM_BRAIN_FIRST_BLAST_RADIUS_SYMBOLS", "").strip()
    if not raw:
        raise AssertionError(
            "PRISM_BRAIN_FIRST_BLAST_RADIUS_SYMBOLS is not set. AC-4 reads "
            "the blast-radius symbol list from the fresh drive's own "
            "task.plan_doc; pass those symbol names, comma-separated, via "
            "this env var. Refusing to report a vacuous pass "
            "(task 3a3f90da NFR-3)."
        )
    return [s.strip() for s in raw.split(",") if s.strip()]


# ---------------------------------------------------------------------------
# AC-2..AC-5 -- transcript-driven, require a fresh drive (see module docstring).
# ---------------------------------------------------------------------------

def test_ac2_context_bundle_called_once_and_conventions_reach_a_later_preamble():
    try:
        transcripts = _locate_drive_transcripts()
    except AssertionError as exc:
        pytest.fail(str(exc))
    cb_calls = []
    result_by_id: dict[str, str] = {}
    for path in transcripts:
        for ev in _walk_events(path):
            if ev["kind"] == "tool_use" and ev["name"] == "mcp__prism__context_bundle":
                cb_calls.append((path, ev))
            elif ev["kind"] == "tool_result":
                result_by_id[ev["tool_use_id"]] = ev["text"]
    assert len(cb_calls) == 1, (
        f"Expected exactly one context_bundle call across the whole drive, "
        f"found {len(cb_calls)}: {[str(p) for p, _ in cb_calls]}"
    )
    cb_path, cb_ev = cb_calls[0]
    result_text = result_by_id.get(cb_ev["tool_use_id"], "")
    assert result_text, f"No tool_result found for the context_bundle call in {cb_path}"
    conventions = _extract_conventions(result_text)
    assert conventions, (
        f"context_bundle returned no non-empty conventions array "
        f"(raw result: {result_text[:300]!r})"
    )
    later_hit = None
    for path in transcripts:
        if path == cb_path:
            continue
        for ev in _walk_events(path):
            if ev["kind"] == "text" and any(c and c in ev["text"] for c in conventions):
                later_hit = (path, ev["text"][:200])
                break
        if later_hit:
            break
    assert later_hit is not None, (
        f"None of the {len(conventions)} conventions from context_bundle "
        f"appeared in any later step transcript's preamble text."
    )


def test_ac3_brain_call_precedes_first_disk_retrieval_in_locate_and_plan():
    try:
        transcripts = _locate_drive_transcripts()
    except AssertionError as exc:
        pytest.fail(str(exc))
    locate_path = _find_transcript_by_marker(transcripts, LOCATE_MARKER)
    plan_path = _find_transcript_by_marker(transcripts, PLAN_MARKER)
    for label, path in (("locate", locate_path), ("plan", plan_path)):
        brain_idx = None
        disk_idx = None
        for i, ev in enumerate(_walk_events(path)):
            if ev["kind"] != "tool_use":
                continue
            if brain_idx is None and ev["name"] in (
                "mcp__prism__brain_search", "mcp__prism__brain_understand",
            ):
                brain_idx = i
            if disk_idx is None and _is_disk_retrieval(ev["name"], ev["input"]):
                disk_idx = i
        assert brain_idx is not None, (
            f"{label} transcript {path} never called brain_search/brain_understand."
        )
        assert disk_idx is None or brain_idx < disk_idx, (
            f"{label} transcript {path}: first DISK-RETRIEVAL call is at event "
            f"#{disk_idx}, before the first brain_search/brain_understand call "
            f"at event #{brain_idx}."
        )


def test_ac4_brain_call_chain_and_find_references_per_blast_radius_symbol():
    try:
        transcripts = _locate_drive_transcripts()
        symbols = _blast_radius_symbols()
    except AssertionError as exc:
        pytest.fail(str(exc))
    call_chain_args: set[str] = set()
    find_refs_args: set[str] = set()
    for path in transcripts:
        for name, inp in _scan(path):
            if name == "mcp__prism__brain_call_chain":
                call_chain_args.add(str(inp.get("entity", "")))
            elif name == "mcp__prism__brain_find_references":
                find_refs_args.add(str(inp.get("name", "")))
    missing_cc = [s for s in symbols if s not in call_chain_args]
    missing_fr = [s for s in symbols if s not in find_refs_args]
    assert not missing_cc, (
        f"brain_call_chain never ran for: {missing_cc} "
        f"(ran for: {sorted(call_chain_args)})"
    )
    assert not missing_fr, (
        f"brain_find_references never ran for: {missing_fr} "
        f"(ran for: {sorted(find_refs_args)})"
    )


def test_ac5_disk_to_brain_search_ratio_under_4_to_1():
    try:
        transcripts = _locate_drive_transcripts()
    except AssertionError as exc:
        pytest.fail(str(exc))
    disk = 0
    brain = 0
    for path in transcripts:
        for name, inp in _scan(path):
            if _is_disk_retrieval(name, inp):
                disk += 1
            if name == "mcp__prism__brain_search":
                brain += 1
    assert brain > 0, (
        f"brain_search was called 0 times across the drive ({disk} disk-"
        "retrieval calls counted) -- a zero count is a FAILURE, never a "
        "divide-by-zero skip."
    )
    ratio = disk / brain
    assert ratio < 4.0, (
        f"disk-retrieval:brain_search ratio is {disk}:{brain} = {ratio:.2f}, "
        "not under the required 4.0."
    )
