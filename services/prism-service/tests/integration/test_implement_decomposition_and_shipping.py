"""Pin the three defects the epic 2d480b08 drive exposed in implement.js.

Every assertion here reads the workflow SOURCE WITH COMMENTS STRIPPED. That is
not fussiness: the fix ships with long comments that literally contain every
string worth asserting on (``task.changed``, ``task_updated``, ``shipped``,
``childrenOwnTheBuild``), so a naive substring check over the raw file is green
before a single line of behaviour exists. Three assertions in this repo have
already been satisfied by an explanatory comment; this suite refuses to be the
fourth.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/integration -> tests -> prism-service -> services -> .prism
# (same walk as test_implement_model_tiering.py:34-37)
WORKFLOW = Path(__file__).resolve().parents[4] / ".claude" / "workflows" / "implement.js"


def _code_only(text: str) -> str:
    """Drop /* */ blocks and every `//` comment, whole-line or trailing.

    `(?<!:)` spares `http://` so URLs inside prompt strings survive. This errs
    toward stripping TOO much (a `//` inside a string literal goes too), which
    is the safe direction: over-stripping can only make an assertion fail, and
    a false red is cheap while a false green is what this suite exists to stop.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(re.sub(r"(?<!:)//.*$", "", line) for line in text.splitlines())


@pytest.fixture(scope="module")
def code() -> str:
    assert WORKFLOW.is_file(), f"workflow not found at {WORKFLOW}"
    raw = WORKFLOW.read_text(encoding="utf-8")
    stripped = _code_only(raw)
    # Guard the guard. If comment-stripping ever silently no-ops, every
    # assertion below becomes satisfiable by prose again — so prove the
    # stripper works on a synthetic case AND that it removed something real.
    assert _code_only("keep()\n// drop me\ntail() // drop\n") == "keep()\n\ntail() "
    assert len(stripped) < len(raw), "stripper removed nothing from the workflow"
    return stripped


def test_parent_stops_building_once_children_own_the_slice(code: str) -> None:
    """Defect 1: the epic drove its own implement_tasks to a full
    implementation AND spawned children that rebuilt the same seam."""
    assert "let childrenOwnTheBuild = false" in code, (
        "no childrenOwnTheBuild flag — the parent has no way to know its "
        "children already own the implementation")
    assert "childrenOwnTheBuild = true" in code, (
        "the flag is never set; it must flip where children are actually created")
    assert re.search(r"childrenOwnTheBuild\s*\?", code), (
        "workerPrompt must branch on the flag, not merely declare it")


def test_the_assembly_instruction_reaches_the_worker_prompt(code: str) -> None:
    """The flag is worthless unless the instruction is actually emitted into
    the prompt the worker reads."""
    assert "assembleOnly" in code, "no assembly-only instruction block"
    assert re.search(r"\.\.\.assembleOnly", code), (
        "assembleOnly is built but never spread into the returned prompt array")
    # Misfire guard: a failed child must not strand its slice. The parent has
    # to retain permission to fill a real gap, naming it.
    assert "gap" in code.lower(), (
        "assembly instruction must still permit filling a genuine gap no child "
        "covered, otherwise a failed child strands work permanently")


def test_the_shared_seam_is_named_once_for_all_children(code: str) -> None:
    """Defect 2: two children invented `task_updated` and `task.changed` for
    the same event and both went green; disjoint allowed_files did not help
    because the collision was in the NAMES."""
    assert "shared_contract" in code, (
        "DECOMPOSE_SCHEMA has no shared_contract — nothing fixes the seam names")
    # It must be a real schema property, not only prose in the prompt.
    assert re.search(r"shared_contract:\s*\{", code), (
        "shared_contract must be a schema property the decomposer is forced to fill")


def test_settle_separates_verified_from_shipped(code: str) -> None:
    """Defect 3: the epic closed status=done, full_outcome_complete=true, while
    its PR was open and origin/main contained none of it."""
    assert re.search(r"shipped:\s*\{", code), (
        "SETTLE_SCHEMA must carry a `shipped` field")
    assert re.search(r"shipping_state:\s*\{", code), (
        "SETTLE_SCHEMA must carry `shipping_state` saying where the work is")
    assert "merge-base --is-ancestor" in code, (
        "shipped must be decided by git ancestry against origin/main, not by "
        "the gate — the epic-rollup adapter has no shipping tooth at all")


def test_the_report_cannot_be_read_as_shipped(code: str) -> None:
    """A caller that reads `done` must not be able to mistake it for shipped."""
    assert re.search(r"^\s*shipped:\s*!!\(", code, re.M), (
        "the returned report must expose `shipped` as its own top-level field")
    assert "verified_not_released" in code, (
        "the report must name the verified-but-unmerged state explicitly")


def test_evidence_must_be_cited_not_merely_written(code: str) -> None:
    """measured_before_after.md sat in the evidence store with cited=false, so
    it never reached the gate card and the owner nearly signed without it."""
    assert re.search(r"evidence_cited:\s*\{", code), (
        "SETTLE_SCHEMA must report each evidence file's cited flag")
    assert "cited" in code, "settle must actually check the cited flag"
