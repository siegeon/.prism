"""Workflow-behavior content: the Workflows page's step-detail panel must
explain WHO may decide a gate, and how to recover from a wrong decision
(owner 2026-08-25, task 3baadd19: "prevent it with workflow behavior
content" -- prevention via visible in-app documentation, not tribal
knowledge relayed in chat). Backed by GET /api/workflows' new `authority`
field (test_gates_carry_authority_content_naming_the_recovery_lever,
test_api_workflows.py) -- this file pins that the SPA actually RENDERS it.

The PRISM SPA has NO JS test runner, so UI acceptance criteria are pinned
by asserting the ACTUAL TSX source (tests/unit/test_conductor_page_
animated_cleanup_ui.py:4-6). Comments are stripped before every assertion
(repo convention, CLAUDE.md Lessons e139295d).
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_TSX = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
       / "WorkflowsPage.tsx")
_TYPES_TS = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "lib"
            / "useWorkflowDef.ts")


def _strip_comments(src: str) -> str:
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(?<!:)(?<!\\)//.*$", "", src)
    return src


def _authority_block() -> str:
    assert _TSX.exists(), f"{_TSX} missing"
    src = _strip_comments(_TSX.read_text(encoding="utf-8"))
    start = src.index("{selectedStep.authority && (")
    end = src.index('aria-label="Step behavior"', start)
    return src[start:end]


def test_step_detail_type_carries_the_authority_field():
    assert _TYPES_TS.exists(), f"{_TYPES_TS} missing"
    src = _strip_comments(_TYPES_TS.read_text(encoding="utf-8"))
    step_type = src[src.index("export type WorkflowStepDef"):]
    step_type = step_type[:step_type.index("};") + 2]
    assert "authority" in step_type, (
        f"WorkflowStepDef must declare authority, or the API's new field "
        f"is invisible to TypeScript callers: {step_type}"
    )


def test_gate_authority_note_renders_when_present():
    block = _authority_block()
    assert "selectedStep.authority" in block, (
        f"the step-detail panel must render selectedStep.authority: {block}"
    )
    assert "Who decides this gate" in block, (
        "the note must be legibly labelled, not a bare unexplained "
        f"string dump: {block}"
    )


def test_authority_note_is_conditionally_rendered_not_always_shown():
    """Non-gate steps carry an empty authority string (workflows.py's
    GATE_AUTHORITY.get(..., "")) -- the note must not render an empty
    "Who decides this gate" box for them."""
    block = _authority_block()
    assert re.search(r"selectedStep\.authority\s*&&", block) or re.search(
        r"\{selectedStep\.authority\s*\?", block), (
        f"the authority note must be conditionally rendered on a "
        f"truthy selectedStep.authority, not unconditionally: {block}"
    )
