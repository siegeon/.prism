"""RED scaffold — step-agent telemetry instruction must forbid a
self-reported "tokens" key (task c740443e).

A step agent hand-builds the POST body for /api/agent-runs/ingest from
the instruction text .claude/workflows/implement.js's telemetryInstr()
returns. That text already forbids a self-claimed "gate_state" field
(the self-approval hole task 682b7e48 closed) but says nothing about
"tokens" -- so a step agent is free to invent one. This pins that the
instruction text explicitly forbids it, isolated to the telemetryInstr
function body so a match landing in an unrelated comment elsewhere in
the file cannot false-green this test.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WORKFLOW_FILE = _REPO_ROOT / ".claude" / "workflows" / "implement.js"


def _telemetry_instr_body() -> str:
    text = _WORKFLOW_FILE.read_text(encoding="utf-8")
    match = re.search(r"function telemetryInstr\([^)]*\)\s*\{", text)
    assert match, (
        f"could not find `function telemetryInstr(...)` in {_WORKFLOW_FILE}"
    )
    start = match.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, "unterminated telemetryInstr function body"
    return text[start:i]


def test_workflow_file_exists():
    assert _WORKFLOW_FILE.is_file(), f"expected workflow file at {_WORKFLOW_FILE}"


def test_telemetry_instruction_forbids_a_self_reported_tokens_field():
    body = _telemetry_instr_body()
    assert '"tokens"' in body, (
        "telemetryInstr's returned instruction text must explicitly "
        "mention a \"tokens\" field so a step agent knows never to "
        "self-report one -- token accounting is harness-tracked, not "
        "claimed by the model being metered"
    )
    lowered = body.lower()
    forbidding_phrases = (
        "never include a \"tokens\"",
        "never add a \"tokens\"",
        "do not add a \"tokens\"",
        "do not include a \"tokens\"",
    )
    assert any(p in lowered for p in forbidding_phrases), (
        "telemetryInstr must explicitly forbid a self-reported \"tokens\" "
        f"key (mirroring its existing gate_state prohibition); body was:\n{body}"
    )
