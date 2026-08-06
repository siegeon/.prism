"""The plan stop is ONE click on what you can SEE (task 7feed0c8).

Owner directive 2026-08-04: "you should track the name not me... the
/prototype implementation should be the stop where i can see the plan your
components or whatever, when the plan is approved then we should let ai
drive all the way to the final implemented green gate."

The PRISM SPA has NO JS test runner, so UI acceptance criteria are pinned by
asserting the ACTUAL TSX source (convention:
tests/unit/test_conductor_page_animated_cleanup_ui.py:4-6).

Two hard-won rules shape the helpers below (CLAUDE.md Lessons):
  * an explanatory COMMENT must never satisfy an assertion, so every check
    runs against comment-stripped source;
  * never slice a fixed character window around a match - parse the
    ENCLOSING JSX branch condition, because a comment above an element
    silently pushes the real guard out of a fixed window.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SVC = _HERE.parent.parent.parent
_SRC = _SVC / "prism_service" / "web" / "src"
_PACKET = _SRC / "components" / "plan" / "DesignPacket.tsx"
_PLANVIEW = _SRC / "components" / "plan" / "PlanView.tsx"
_CONDUCTOR_API = _SVC / "prism_service" / "api" / "conductor.py"


def _strip_comments(src: str) -> str:
    """Drop // line comments and /* */ blocks. A comment explaining where a
    control USED to live must never satisfy an assertion about the control."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def _jsx_branch_for(src: str, marker: str) -> str:
    """Return the enclosing `{<condition> && (` guard for a render site.

    Walks BACKWARD from the marker to the nearest `&& (` that opens a JSX
    branch and returns the condition text. Never a fixed window."""
    at = src.index(marker)
    head = src[:at]
    opens = [m for m in re.finditer(r"\{([^{}]{0,400}?)&&\s*\(", head, flags=re.S)]
    if not opens:
        raise AssertionError(f"no enclosing JSX branch found for {marker!r}")
    return opens[-1].group(1).strip()


@pytest.fixture(scope="module")
def packet() -> str:
    return _strip_comments(_PACKET.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def planview() -> str:
    return _strip_comments(_PLANVIEW.read_text(encoding="utf-8"))


# ---- AC-1: nothing to type -------------------------------------------


def test_no_free_text_approver_input(packet: str) -> None:
    """The rendered element, not the identifier: no `your name` text box."""
    inputs = re.findall(r"<input\b[^>]*>", packet, flags=re.S)
    offenders = [i for i in inputs if "your name" in i]
    assert not offenders, f"free-text approver input still rendered: {offenders}"
    assert "setApprover" not in packet, "the approver useState still exists"


def test_do_approve_does_not_refuse_on_local_state(packet: str) -> None:
    """doApprove must not dead-end on an empty local string."""
    assert "name yourself as the approver" not in packet
    assert not re.search(r"!\s*approver\.trim\(\)", packet), \
        "doApprove still refuses on an empty local approver"


# ---- AC-2/AC-3: the identity is supplied, never dropped ---------------


def test_approver_comes_from_the_session(packet: str) -> None:
    """Same source the page header's IdentityChip reads (PageHeader.tsx:36)."""
    assert "/api/auth/me" in packet, "the card never resolves the session identity"
    body = re.search(r"design-packet/approve[^)]*\)\s*,\s*(\{[^}]*\})", packet, flags=re.S)
    assert body, "could not find the approve POST body"
    assert "approver" in body.group(1)
    assert "approver: approver" not in body.group(1), \
        "the POST body still reads a bare useState string"


def test_server_resolves_the_approver_from_the_principal() -> None:
    """Defence in depth: the route fills a blank approver from the principal."""
    src = _CONDUCTOR_API.read_text(encoding="utf-8")
    fn = src[src.index("def design_packet_approve"):]
    fn = fn[:fn.index("\n@router")] if "\n@router" in fn else fn
    assert "current_principal" in fn, "the route does not resolve a principal"
    for placeholder in ('"owner"', "'owner'", '"Local User"', '"unknown"'):
        assert placeholder not in fn, f"approvals signed by a placeholder {placeholder}"


def test_record_approval_still_requires_an_identity() -> None:
    """stop_if #1: SUPPLY the identity, never stop requiring one."""
    from prism_service.services import design_packet as dp

    assert dp._ALLOWED_METHODS == {"owner_explicit"}
    for empty in ("", "   "):
        with pytest.raises(ValueError):
            dp.record_approval("prism", "t", object(), approver=empty,
                               method="owner_explicit")


# ---- AC-4/AC-5: decision first, artifact above the prose --------------


def _prose_at(packet: str) -> int:
    return packet.index("proposed change")


def test_decision_is_rendered_before_the_prose(packet: str) -> None:
    assert packet.index("Approve design") < _prose_at(packet), \
        "the approve control still sits below the plan prose"


def test_prototype_branch_is_above_the_prose(packet: str) -> None:
    """Assert the BRANCH, not the mere presence of the markup."""
    cond = _jsx_branch_for(packet, '<iframe\n              title="prototype"') \
        if '<iframe\n              title="prototype"' in packet \
        else _jsx_branch_for(packet, 'title="prototype"')
    assert "data.prototype.exists" in cond, f"prototype guard drifted: {cond!r}"
    assert packet.index('title="prototype"') < _prose_at(packet)


def test_diagram_branch_is_above_the_prose(packet: str) -> None:
    cond = _jsx_branch_for(packet, "<Mermaid")
    assert "plan_diagram" in cond, f"diagram guard drifted: {cond!r}"
    assert packet.index("<Mermaid") < _prose_at(packet)


# ---- AC-9: you LAND on the decision, you do not hunt for it -----------


def test_plan_gate_lands_on_the_design_tab(planview: str) -> None:
    """The affordance a person USES (CLAUDE.md: e139295d).

    A gate parks the task, which makes hasImpl true, which used to auto-open
    the Implementation tab - hiding the approval card behind a tab the
    reviewer never lands on. At a PENDING gate the Design tab must win."""
    init = re.search(r"useState\(\s*(has\w+[^)]*)\)", planview)
    assert init, "could not find the active-tab initialiser"
    assert "gateState" in init.group(1) or "awaitingDesign" in init.group(1), \
        f"the landing tab still ignores the gate: {init.group(1)!r}"
