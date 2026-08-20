"""Pins task bbfd1a19 ("Validation runs tests and builds"): verify_green_state
must explicitly run BOTH a test lane and a build/typecheck lane from
task.verify, not just the test suite.

The PRISM SPA/workflow scripts have NO JS test runner, so this JS prompt
behavior is pinned by asserting on the ACTUAL source text of
`.claude/workflows/implement.js` — the same convention as
tests/unit/test_conductor_page_animated_cleanup_ui.py.

Today STEP_EXTRA.verify_green_state (implement.js) only says to run "the
FULL relevant suite" and report red as outcome="fail" — it never names a
build/typecheck lane, never asks for classification of task.verify entries,
never requires separate per-lane evidence, and never guards against
inventing an undeclared build step. All assertions below FAIL against that
text. They pass only once verify_green_state is extended per AC-1..AC-4 of
task bbfd1a19.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent.parent.parent.parent
_WORKFLOW = _REPO_ROOT / ".claude" / "workflows" / "implement.js"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _verify_green_state_block() -> str:
    """Scope to the verify_green_state entry inside STEP_EXTRA, not the
    whole file — a keyword landing in a sibling step (e.g. write_failing_tests)
    must not satisfy these assertions."""
    src = _read(_WORKFLOW)
    m = re.search(
        r"verify_green_state:\s*\[(.*?)\]\.join\('\\n'\),",
        src,
        re.DOTALL,
    )
    assert m, (
        "STEP_EXTRA.verify_green_state array literal not found in "
        f"{_WORKFLOW} — did the step key or the STEP_EXTRA shape change?"
    )
    return m.group(1)


# ---------------------------------------------------------------------------
# AC-1: classify every task.verify entry into a TEST lane or a BUILD/TYPECHECK
# lane by command shape.
# ---------------------------------------------------------------------------

def test_verify_green_state_classifies_test_and_build_lanes():
    block = _verify_green_state_block()
    assert re.search(r"classif", block, re.I), \
        "verify_green_state must instruct the agent to CLASSIFY task.verify entries"
    assert re.search(r"\btest\b[\s-]*lane", block, re.I), \
        "verify_green_state must name an explicit TEST lane"
    assert re.search(r"build[/\s-]*(and[/\s-]*)?typecheck[\s-]*lane|build[\s-]*lane", block, re.I), \
        "verify_green_state must name an explicit BUILD/TYPECHECK lane"


# ---------------------------------------------------------------------------
# AC-2: run every declared entry in both lanes with separate captured
# pass/fail evidence per lane, not one undifferentiated "tests passed" line.
# ---------------------------------------------------------------------------

def test_verify_green_state_requires_separate_evidence_per_lane():
    block = _verify_green_state_block()
    assert re.search(r"separate", block, re.I) and re.search(r"lane", block, re.I), \
        "verify_green_state must require SEPARATE evidence per lane"
    assert re.search(r"evidence|exit code|output", block, re.I), \
        "verify_green_state must require captured pass/fail evidence, not a single summary line"


# ---------------------------------------------------------------------------
# AC-3: reporting success is blocked if EITHER lane fails — extends the
# existing "report red as outcome=fail" rule to explicitly cover build too.
# ---------------------------------------------------------------------------

def test_verify_green_state_blocks_success_if_either_lane_fails():
    block = _verify_green_state_block()
    assert re.search(r"either\s+lane", block, re.I) or re.search(r"both\s+lanes", block, re.I), \
        "verify_green_state must explicitly block success if EITHER lane (test or build) fails"


# ---------------------------------------------------------------------------
# AC-4: the build/typecheck lane is scoped only to entries task.verify
# actually declares — a test-only task must never be forced to invent one.
# ---------------------------------------------------------------------------

def test_verify_green_state_never_invents_an_undeclared_build_step():
    block = _verify_green_state_block()
    assert re.search(r"(never|do not|don't)\s+invent", block, re.I), \
        "verify_green_state must guard against INVENTING a build/typecheck step"
    assert re.search(r"declar", block, re.I), \
        "the invent-guard must be tied to what task.verify actually DECLARES"
