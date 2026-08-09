"""UI contract tests for "Gate cards stop claiming no evidence at plan_gate"
(task 0c49c385-756a-453c-b707-484d74653e0d).

Owner hit this 2026-08-07 reviewing task e948008a: at plan_gate, a POPULATED
design packet (plan_doc + plan_diagram already written, sitting there waiting
for the owner's own approval click) rendered the notification-banner headline
as "BLOCKED · evidence not on file" - the exact string reserved for a
green_gate with NO EvidenceReceipt on file at all. At plan_gate,
receipt_ok=false means "not yet approved by you", never "no evidence exists":
GET /api/conductor/gate/readiness (api/conductor.py:171-188) returns
receipt={adapter: "design-packet", passed: false, reason: "...needs an
explicit owner approval..."} for exactly this case, and the frontend headline
ternary (TaskDetailPage.tsx:1550-1555) had no branch for it, so it fell
through to the false, alarming, green_gate-shaped string.

The PRISM SPA has NO JS test runner, so UI acceptance criteria are pinned by
asserting the ACTUAL TSX source, the same convention used by
test_gate_banner_honest_state_ui.py (itself following
test_conductor_page_animated_cleanup_ui.py). Per that convention, this parses
the BALANCED {...} JSX EXPRESSION that follows a literal marker - never a
fixed character window - reusing _extract_braced_expr verbatim
(test_gate_banner_honest_state_ui.py:51-67) so a stray comment or an
unrelated string elsewhere in the file cannot satisfy an assertion the way a
comment above an element has three separate times before.

ALL of these FAIL against the current source: TaskDetailPage.tsx defines no
`isAwaitingDesignApproval` / `packetParts` consts, and the headline ternary
has only four branches (READY, AWAITING YOUR REVIEW, BLOCKED verifier
rejected, BLOCKED evidence not on file) - none of which distinguish a
populated-but-unapproved design packet from truly-no-evidence. They go green
only once TaskDetailPage.tsx adds a leading ternary branch, ahead of the
existing ready/blocked branches, that keys on
`gateReadiness?.receipt?.adapter === "design-packet"` (never on
`task.workflow_step` string equality - the next non-evidence gate must not
inherit the same false message) plus `passed === false` plus the packet
actually having content (plan_doc or plan_diagram non-empty - a genuinely
empty packet must keep falling through to the existing missing-content
copy), and names which packet parts are present.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_TASK_DETAIL = _SRC / "pages" / "TaskDetailPage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Strip // line comments and /* */ block comments so a comment cannot
    satisfy an assertion the way one has three separate times before (see
    TaskDetailPage.tsx CLAUDE.md lesson on RENDERED TAG / branch parsing).
    Deliberately naive (no string-literal awareness) - adequate for scanning
    TSX source that does not embed '//' or '/*' inside the strings we scan
    near, matching the level of rigor the neighbouring suite already uses."""
    no_block = re.sub(r"/\*[\s\S]*?\*/", "", src)
    no_line = re.sub(r"//[^\n]*", "", no_block)
    return no_line


def _extract_braced_expr(src: str, marker: str) -> str:
    """Return the JSX `{...}` expression that starts at the first `{` after
    `marker`, matched by BRACE DEPTH (not a fixed slice) so the assertion
    can't be satisfied by a comment or an unrelated literal near the marker
    instead of the real guard that decides what renders. Verbatim convention
    from test_gate_banner_honest_state_ui.py:51-67."""
    idx = src.index(marker)
    start = src.index("{", idx)
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces scanning forward from marker {marker!r}")


def _const_definition(src: str, name: str) -> str:
    m = re.search(rf"const\s+{name}\s*(?::[^=]*)?=([\s\S]*?);\n", src)
    assert m, (
        f"TaskDetailPage.tsx must define a `{name}` const that the gate "
        f"banner headline (and tone) key on"
    )
    return m.group(1)


# ---------------------------------------------------------------------------
# FR-2 / NFR-1 - packetParts names which packet parts are present, derived
# from task fields, never from workflow_step string equality.
# ---------------------------------------------------------------------------

def test_packet_parts_names_what_is_present_from_task_fields():
    src = _strip_comments(_read(_TASK_DETAIL))
    definition = _const_definition(src, "packetParts")

    assert "plan_doc" in definition, (
        "packetParts must derive from task.plan_doc, one of the three "
        "packet parts the design packet is assembled from"
    )
    assert "plan_diagram" in definition, (
        "packetParts must derive from task.plan_diagram"
    )
    assert "has_prototype" in definition, (
        "packetParts must derive from task.has_prototype"
    )
    assert "workflow_step" not in definition, (
        "packetParts must key off task fields, not a workflow_step string "
        "equality check"
    )


# ---------------------------------------------------------------------------
# FR-1 / FR-3 / NFR-1 - isAwaitingDesignApproval keys on the readiness
# receipt's adapter, never workflow_step, and requires real packet content.
# ---------------------------------------------------------------------------

def test_is_awaiting_design_approval_keys_on_adapter_not_workflow_step():
    src = _strip_comments(_read(_TASK_DETAIL))
    definition = _const_definition(src, "isAwaitingDesignApproval")

    assert '"design-packet"' in definition, (
        "isAwaitingDesignApproval must check "
        'gateReadiness?.receipt?.adapter === "design-packet" - the signal '
        "the backend already sends (api/conductor.py:177,185) - never "
        "task.workflow_step string equality (NFR-1: the next non-evidence "
        "gate must not inherit the same false message)"
    )
    assert "adapter" in definition, (
        "isAwaitingDesignApproval must read gateReadiness.receipt.adapter"
    )
    assert "workflow_step" not in definition, (
        "isAwaitingDesignApproval must NOT key on task.workflow_step "
        "string equality (NFR-1) - that would let the next non-evidence "
        "gate inherit the same false message"
    )
    assert "passed" in definition, (
        "isAwaitingDesignApproval must check receipt.passed === false - "
        "the live signal that the packet is not yet approved"
    )

    # FR-3: a genuinely empty design packet (plan_doc AND plan_diagram both
    # empty) must still fall through to the existing missing-content copy -
    # only the populated-but-unapproved case gets the new copy. So the
    # const's own definition must reference the packet-content fields, not
    # just the receipt.
    assert "plan_doc" in definition or "packetParts" in definition, (
        "isAwaitingDesignApproval must require actual packet content "
        "(plan_doc or plan_diagram non-empty) so a genuinely empty packet "
        "still falls through to the existing missing-content copy (FR-3)"
    )


# ---------------------------------------------------------------------------
# FR-1 / FR-4 / FR-5 / FR-6 - the headline ternary gets a new leading branch,
# the green_gate 'evidence not on file' literal stays reachable and
# byte-for-byte unchanged, and it renders in the calm-amber tone.
# ---------------------------------------------------------------------------

def test_headline_gains_a_leading_awaiting_design_approval_branch():
    src = _strip_comments(_read(_TASK_DETAIL))
    expr = _extract_braced_expr(src, "● {")

    assert "isAwaitingDesignApproval" in expr, (
        "the headline expression must branch on isAwaitingDesignApproval"
    )
    assert "AWAITING YOUR APPROVAL" in expr, (
        "a populated-but-unapproved design packet must render an honest "
        "'AWAITING YOUR APPROVAL' headline, not the green_gate-shaped "
        "'evidence not on file'"
    )
    assert "packetParts" in expr, (
        "FR-2: the awaiting-approval string must name which packet parts "
        "are present (derived from packetParts)"
    )

    # FR-4: the design-packet-populated-and-unapproved branch is evaluated
    # FIRST in the ternary, so the literal 'evidence not on file' string is
    # UNREACHABLE on that branch. In a JS ternary written top-down, "first"
    # means the isAwaitingDesignApproval condition's own text appears before
    # the 'evidence not on file' literal in source order.
    approval_idx = expr.index("isAwaitingDesignApproval")
    blocked_idx = expr.index("evidence not on file")
    assert approval_idx < blocked_idx, (
        "isAwaitingDesignApproval must be checked BEFORE the "
        "'evidence not on file' branch is reached (FR-4: that literal must "
        "be unreachable on the design-packet-populated-and-unapproved "
        "branch, which requires evaluating it first in the ternary)"
    )

    # FR-5: the green_gate path is byte-for-byte unchanged - the exact
    # literal must still be present, verbatim, for the true no-evidence case.
    assert "BLOCKED · evidence not on file" in expr, (
        "FR-5: a green_gate with no EvidenceReceipt must still render "
        "'BLOCKED · evidence not on file' verbatim - this slice must not "
        "touch that branch's text"
    )
    assert "BLOCKED · verifier rejected current evidence" in expr, (
        "the pre-existing verifier-refusal branch must be preserved"
    )
    assert "READY · evidence passing" in expr, (
        "the pre-existing affirmative branch must be preserved"
    )
    assert "AWAITING YOUR REVIEW" in expr, (
        "the pre-existing human-review-entitlement branch (task 72d3e0d1) "
        "must be preserved"
    )

    # The new awaiting-approval headline text itself must not contain
    # 'BLOCKED' or claim the packet is missing - bound to its own line so a
    # different branch's text can't satisfy this negatively.
    approval_text_idx = expr.index("AWAITING YOUR APPROVAL")
    line_start = expr.rfind("\n", 0, approval_text_idx) + 1
    line_end = expr.index("\n", approval_text_idx) if "\n" in expr[approval_text_idx:] else len(expr)
    approval_line = expr[line_start:line_end]
    assert "BLOCKED" not in approval_line, (
        "the awaiting-design-approval headline must not itself read as "
        "BLOCKED - awaiting an owner approval is a correct state (FR-6)"
    )


def test_awaiting_design_approval_renders_in_calm_amber_never_rose():
    src = _strip_comments(_read(_TASK_DETAIL))
    definition = _const_definition(src, "bannerTone")

    assert "isAwaitingDesignApproval" in definition, (
        "FR-6: bannerTone must key on isAwaitingDesignApproval so the "
        "awaiting-approval state gets its own tone"
    )
    assert '"amber"' in definition, "the awaiting-approval tone must be amber"

    # Bound: the isAwaitingDesignApproval branch of the ternary must resolve
    # to amber, not rose - slice from its own mention up to the next '?' or
    # ':' branch separator so a distant 'rose' elsewhere can't satisfy this.
    idx = definition.index("isAwaitingDesignApproval")
    tail = definition[idx:]
    # the branch immediately following the condition, up to its own ':' or
    # the next 200 chars, whichever is shorter
    colon_idx = tail.find(":")
    window_end = min(len(tail), colon_idx if colon_idx != -1 else 200, 200)
    branch_window = tail[:window_end]
    assert '"amber"' in branch_window or "isAwaitingDesignApproval ?" in tail[:40], (
        "isAwaitingDesignApproval must resolve to the amber tone, not rose"
    )
