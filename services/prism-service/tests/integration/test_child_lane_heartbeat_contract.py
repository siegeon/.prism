"""Child lanes report a live heartbeat (task 774f11d1).

childDriverPrompt (.claude/workflows/implement.js) drives a full nested SDLC
per decomposed child slice and is the LONGEST-running agent in the whole
flow, yet unlike workerPrompt/claimFirstInstr/the Graph prompt it carries no
drive-heartbeat instruction -- only the one-shot, end-of-step
telemetryInstr('child_drive', 'dev'). Every child of a decomposed epic reads
adrift/stalled with heartbeat:null on the live board for its ENTIRE drive.

Source-scans childDriverPrompt's own return value, comments stripped, so a
match sitting in a neighbouring prompt (workerPrompt/claimFirstInstr/Graph)
or in a JS comment cannot satisfy these assertions (the task's own
likely_misfire names exactly that failure mode).

Covers:
  AC-1 -- the /api/drive-heartbeat/beat POST contract, with the
          first-beat-immediately + re-beat-every-5-8-tool-calls cadence, is
          present in childDriverPrompt's returned text.
  AC-2 -- the beat's task_id interpolates the CHILD's own id (child.task_id),
          never a parent/epic-scoped identifier.
  AC-3 -- the beat's step is sourced from the lane's own LIVE current step,
          not one value fixed at prompt-generation time.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WORKFLOW = _REPO_ROOT / ".claude" / "workflows" / "implement.js"


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    lines = []
    for line in src.splitlines():
        if line.strip().startswith("//"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _child_driver_prompt_body() -> str:
    src = _strip_comments(_WORKFLOW.read_text(encoding="utf-8"))
    start = src.index("function childDriverPrompt(")
    open_at = src.index("{", start)
    depth = 0
    i = open_at
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError("childDriverPrompt's closing brace was never found")


def test_child_driver_prompt_carries_the_heartbeat_contract():
    body = _child_driver_prompt_body()
    assert "/api/drive-heartbeat/beat" in body, (
        "childDriverPrompt must POST to the drive-heartbeat endpoint - "
        "without it every child lane reads dead on the board for its "
        "entire drive"
    )
    assert "5-8" in body, (
        "the re-beat cadence (every 5-8 tool calls) must be present, "
        "matching the contract heartbeatInstr already gives the parent "
        "Drive/Locate/Graph steps"
    )
    assert "first" in body.lower() and "beat" in body.lower(), (
        "the first-beat-immediately instruction must be present"
    )


def test_child_driver_prompt_beats_with_the_childs_own_task_id():
    body = _child_driver_prompt_body()
    assert "/api/drive-heartbeat/beat" in body
    heartbeat_at = body.index("/api/drive-heartbeat/beat")
    # Scope the task_id check to the heartbeat curl payload itself, not the
    # unrelated telemetry POST also present in this function.
    window = body[heartbeat_at:heartbeat_at + 1200]
    assert "child.task_id" in window, (
        "the heartbeat's task_id must interpolate the CHILD's own id "
        "(child.task_id) - interpolating the parent/epic's id would make "
        "the epic tile read driving while the child rows still read dead"
    )
    assert "locate.task_id" not in window and "${TASK_ID}" not in window, (
        "the heartbeat must not fall back to the parent-scoped "
        "locate.task_id/TASK_ID identifiers used by heartbeatInstr for the "
        "PARENT's own step prompts"
    )


def test_child_driver_prompt_steps_the_beat_as_the_lane_crosses_steps():
    body = _child_driver_prompt_body()
    heartbeat_at = body.index("/api/drive-heartbeat/beat")
    window = body[heartbeat_at:heartbeat_at + 1200]
    # The lane must be told to source its OWN live current step at beat
    # time - not have a single step string frozen into the prompt before
    # its internal multi-step conductor_work loop even starts.
    assert re.search(r"job\.step|current\s+step|own\s+step", window, re.I), (
        "the heartbeat instruction must tell the agent to report its OWN "
        "live current conductor step at beat time (e.g. its own job.step), "
        "since childDriverPrompt is generated ONCE per child before that "
        "child's internal multi-step loop begins"
    )
